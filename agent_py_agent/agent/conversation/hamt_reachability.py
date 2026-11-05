"""不可变索引的有界可达性标记；不完整的根绝不产生清扫许可。"""

from __future__ import annotations

import os
import stat
import time
from collections import deque
from collections.abc import Iterable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common import nofollow_fs as fs
from ..user_space.storage_lanes import (
    STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
    STORAGE_LANE_SLICE_BYTES,
    STORAGE_LANE_SLICE_ENTRIES,
    STORAGE_LANE_SLICE_MS,
    StorageLaneBudget,
    lane_cursor_window,
)
from .store_io import ImmutableJsonIndex

# 不可达对象至少保留七天，保护旧读取快照及尚未发表的新根。
HAMT_SWEEP_GRACE_SECONDS = 7 * 86400
# 每片首个线程独享的标记时间上限，避免大提交链反复在常规短片中从头失败。
HAMT_MARK_THREAD_MS = 5000
# 每片首个线程独享的读取额度；全部读量仍进入回执，由调度器阻止后续通道透支。
HAMT_MARK_THREAD_BYTES = 256 * 1024 * 1024
# 单线程完整标记允许的最大节点数，超限只能保留整树并跳过该线程。
HAMT_MARK_MAX_NODES = 65536


@dataclass(frozen=True)
class MarkResult:
    reachable: frozenset[str]
    complete: bool
    dangling: tuple[str, ...]
    visited: int


# LLM: 只调用原 _load 验证节点；坏引用及预算耗尽都撤销整棵树的删除许可，叶值不作为边。
# 函数用途: 广度优先找出所有给定根真正能到达的索引节点。
def mark_reachable_nodes(
    index: ImmutableJsonIndex, roots: Iterable[dict | None], *, max_nodes: int
) -> MarkResult:
    """广度优先验证真实节点，只跟随 children，entries 始终是领域数据。"""
    queue = deque()
    seen, reachable, dangling = {}, set(), []
    visited, complete = 0, True
    for reference in roots:
        if reference is not None:
            queue.append(reference)
            if len(queue) > max_nodes:
                return MarkResult(frozenset(), False, (), 0)
    while queue:
        reference = queue.popleft()
        try:
            name = reference["ref"]["ref"]
            if not isinstance(name, str):
                raise TypeError("invalid node name")
        except (KeyError, TypeError):
            name = "<invalid-reference>"
        if name in seen:
            if not _same_seal(seen[name], reference):
                complete = False
                dangling.append(name)
            continue
        seen[name] = reference
        if visited >= max_nodes:
            complete = False
            break
        visited += 1
        try:
            node = index._load(reference)
        except _BudgetExceeded:
            raise
        except OSError:
            dangling.append(name)
            complete = False
            continue
        reachable.add(name)
        queue.extend(node.get("children", {}).values())
    return MarkResult(frozenset(reachable), complete, tuple(dangling), visited)


# LLM: Python 的 True==1、1==1.0 不足以证明封印相同；原验证成功后仅复用类型和值都相同的引用。
# 函数用途: 严格比较两份引用，避免坏类型借同名正确引用绕过验证。
def _same_seal(first, second):
    if type(first) is not dict or type(second) is not dict or first != second:
        return False
    if any(type(value) is not type(second[key]) for key, value in first.items()):
        return False
    if isinstance(first.get("ref"), dict):
        return all(type(value) is type(second["ref"][key]) for key, value in first["ref"].items())
    return True


class _Directory(AbstractContextManager):
    """固定完整目录链的 FD；无 dir_fd 平台只采用逐级验证的路径。"""

    # LLM: 仅拆解绝对词法路径，不 resolve、不创建目录。
    # 函数用途: 准备一个不跟随链接的目录句柄。
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.parts = self.path.relative_to(self.path.anchor).parts
        self.fd = None

    # LLM: 打开完整父链的只读 FD，便携分支逐段检查；异常不降级为可信目录。
    # 函数用途: 固定本次操作使用的真实目录。
    def __enter__(self):
        if fs._supports_dir_fd():
            self.fd = fs.open_directory_beneath(self.path.anchor, self.parts, require_dir_fd=True)
        else:
            fs._portable_directory(Path(self.path.anchor), self.parts, create=False)
        return self

    # LLM: 只归还本对象拥有的 FD，不吞掉临界区异常。
    # 函数用途: 结束目录操作并关闭句柄。
    def __exit__(self, _exc_type, _exc, _traceback):
        if self.fd is not None:
            os.close(self.fd)

    # LLM: 只流式读名字，额外内存与目录大小无关；迭代器退出关闭扫描句柄。
    # 函数用途: 逐个读取目录里的名字。
    def names(self):
        with os.scandir(self.fd if self.fd is not None else self.path) as entries:
            yield from (entry.name for entry in entries)

    # LLM: 叶名必须是单组件，stat 不跟随叶链接，不产生写入。
    # 函数用途: 查看一个候选当前的文件身份和时间。
    def stamp(self, name):
        fs._validate_relative_parts((name,))
        return (
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if self.fd is not None
            else (self.path / name).lstat()
        )

    # LLM: 只供已验证候选的写门闭包调用，删除本层叶，不递归。
    # 函数用途: 在固定目录中删除一个文件。
    def unlink(self, name):
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            (self.path / name).unlink()

    # LLM: 只供写门内的隔离及恢复调用，POSIX 两端均固定 FD，便携端此前已验证父链。
    # 函数用途: 在原目录和隔离目录之间移动候选。
    def rename(self, name, other, target):
        if self.fd is not None:
            os.rename(name, target, src_dir_fd=self.fd, dst_dir_fd=other.fd)
        else:
            os.rename(self.path / name, other.path / target)


# LLM: 纯元数据谓词，链接、特殊文件及多链接叶均返回 False。
# 函数用途: 判断候选是否是单链接普通文件。
def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


# LLM: 只在 run_write 内执行，先核身份、再隔离和复核时间；重新采用的对象恢复，不覆盖新叶。
# 函数用途: 安全删除一个仍然陈旧的孤儿，或把已复用的候选放回去。
def _quarantine_one(directory, name, initial, cutoff):
    """扫描在写门外；重命名、复核、删除或恢复全部在同一个写门内。"""
    try:
        current = directory.stamp(name)
    except FileNotFoundError:
        return 0, 0, 0
    if not _regular(current) or (current.st_dev, current.st_ino) != (
        initial.st_dev,
        initial.st_ino,
    ):
        return 0, 0, 0
    if directory.fd is not None:
        try:
            os.mkdir(".gc-quarantine", mode=0o700, dir_fd=directory.fd)
        except FileExistsError:
            directory.stamp(".gc-quarantine")
    else:
        (directory.path / ".gc-quarantine").mkdir(mode=0o700, exist_ok=True)
    with _Directory(directory.path / ".gc-quarantine") as quarantine:
        # 旧隔离叶可能来自中断；未知对象不覆盖、不凭名字授予删除权。
        try:
            quarantine.stamp(name)
        except FileNotFoundError:
            directory.rename(name, quarantine, name)
        else:
            return 0, 0, 0
        final = quarantine.stamp(name)
        if (
            not _regular(final)
            or final.st_mtime >= cutoff
            or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            try:
                directory.stamp(name)
            except FileNotFoundError:
                quarantine.rename(name, directory, name)
            else:
                # 同名已由原内容寻址写者重建，不能覆盖新对象。
                if _regular(final):
                    quarantine.unlink(name)
            return 0, 0, 1
        quarantine.unlink(name)
        return 1, final.st_size, 0


# LLM: 接口不变，只接受调用者完整标记后的命名空间，实际变更全部经 run_write。
# 函数用途: 在条数上限内清理本层过了宽限期的不可达 JSON 文件。
def sweep_unreachable_nodes(
    namespace_dir: Path,
    *,
    reachable: frozenset[str],
    mark_started_at: float,
    now: float,
    max_items: int,
    run_write,
) -> dict:
    """只检查本层 JSON；调用者必须先证明标记完整，链接和目录永不进入删除。"""
    result = _sweep_window(
        namespace_dir,
        reachable=reachable,
        mark_started_at=mark_started_at,
        now=now,
        max_items=max_items,
        run_write=run_write,
        after="",
        allowed=lambda: True,
    )
    return {name: result[name] for name in ("deleted", "freed_bytes", "restored", "items")}


# LLM: 候选选择在写门外，窗口为 O(limit) 内存；返回单名字续点，不保存节点列表。
# 函数用途: 按名字续扫一个有界窗口，避免新孤儿堵住后面的旧孤儿。
def _sweep_window(
    namespace_dir,
    *,
    reachable,
    mark_started_at,
    now,
    max_items,
    run_write,
    after,
    allowed,
    suffix=".json",
    dry_run=False,
):
    """目录名称窗口只存最后一个名字；新节点不重置未追平的续点。"""
    result = {
        "deleted": 0,
        "freed_bytes": 0,
        "restored": 0,
        "items": 0,
        "after": after,
        "complete": False,
        "eligible": False,
    }
    if max_items <= 0:
        return result
    cutoff = min(mark_started_at, now) - HAMT_SWEEP_GRACE_SECONDS
    try:
        with _Directory(namespace_dir) as directory:
            names = lane_cursor_window(
                {"after": after},
                (
                    name
                    for name in directory.names()
                    if name.endswith(suffix) and name not in reachable
                ),
                max_items + 1,
            )
            result["complete"] = len(names) <= max_items
            for name in names[:max_items]:
                if not allowed():
                    result["complete"] = False
                    break
                result["items"] += 1
                result["after"] = name
                try:
                    initial = directory.stamp(name)
                except FileNotFoundError:
                    continue
                if not _regular(initial) or initial.st_mtime >= cutoff:
                    continue
                if dry_run:
                    result["eligible"] = True
                    return result
                deleted, freed, restored = run_write(
                    lambda: _quarantine_one(directory, name, initial, cutoff)
                )
                result["deleted"] += deleted
                result["freed_bytes"] += freed
                result["restored"] += restored
    except FileNotFoundError:
        result["complete"] = True
        return result
    return result


class _BudgetExceeded(OSError):
    """预算不足只能保留对象，不能把半棵树用于删除。"""


class _LaneWork:
    """通道计量只在本栈累积；任何写入仍走原 request.run_write。"""

    # LLM: 派生不超过调度器与固定上限的预算，只保存本次栈内计量和小游标。
    # 函数用途: 初始化一个维护片的预算和统计。
    def __init__(self, request):
        self.request = request
        self.budget = request.budget.capped(
            items=STORAGE_LANE_SLICE_ENTRIES,
            size=STORAGE_LANE_SLICE_BYTES,
            ms=STORAGE_LANE_SLICE_MS,
        )
        self.items = self.size = self.deleted = self.freed = 0
        self.errors, self.skipped = [], {}
        self._allowance_used = False
        self._allowance = None
        state = request.cursor.get("sweep")
        self.sweep_state = (
            dict(state)
            if (
                isinstance(state, dict)
                and set(state) == {"namespace", "after"}
                and all(isinstance(value, str) and len(value) <= 1024 for value in state.values())
            )
            else None
        )
        self.retired_probe = request.cursor.get("retired_probe")

    # LLM: 只有首个实际处理的线程可借用独立额度，显式零条目或零字节预算仍拒绝；不改名称窗口预算。
    # 函数用途: 为完整标记和锁内复核开启本片唯一一次五秒读取额度。
    def begin_thread_allowance(self) -> bool:
        if (
            self._allowance_used
            or self.request.budget.max_items == 0
            or self.request.budget.max_bytes == 0
        ):
            return False
        self._allowance_used = True
        self._allowance = StorageLaneBudget(
            self.items + self.budget.max_items,
            self.size + HAMT_MARK_THREAD_BYTES,
            time.monotonic() + HAMT_MARK_THREAD_MS / 1000,
        )
        return True

    # LLM: 只关闭本栈临时额度，不改累计读量、清扫计量或本片已使用标记。
    # 函数用途: 恢复普通通道预算。
    def end_thread_allowance(self) -> None:
        self._allowance = None

    # LLM: 只选择本片实际额度，不改变名称发现窗口使用的 work.budget。
    # 函数用途: 给预筛和清扫提供一致的条数预算。
    def _active_budget(self):
        return self._allowance or self.budget

    # LLM: 异常与早退均归还临时额度，嵌套或后续调用不会关闭别人的额度。
    # 函数用途: 用上下文确保首线程额度配对关闭。
    @contextmanager
    def thread_allowance(self):
        first = self.begin_thread_allowance()
        try:
            yield first
        finally:
            if first:
                self.end_thread_allowance()

    # LLM: 首线程额度生效时单独计量；其余时间同时检查原预算和常规片预算，零预算不放大。
    # 函数用途: 判断本片是否已经没有额度。
    def exhausted(self):
        if self._allowance is not None:
            return self._allowance.exhausted(self.items, self.size)
        return self.request.budget.exhausted(self.items, self.size) or self.budget.exhausted(
            self.items, self.size
        )

    # LLM: 先验证非负整数，再按当前实际额度计量；不足抛专用异常，绝不授权半棵树清扫。
    # 函数用途: 为一次实际读取占用字节预算。
    def read(self, size):
        if type(size) is not int or size < 0:
            raise OSError("invalid reachability read size")
        timed_out = (
            self._allowance.remaining_ms() <= 0 if self._allowance is not None else
            self.budget.remaining_ms() <= 0 or self.request.budget.remaining_ms() <= 0
        )
        if timed_out or size > self.available_bytes():
            raise _BudgetExceeded("reachability budget exhausted")
        self.size += size

    # LLM: 首线程独立额度只在显式允许时生效，其他时候保留调用方零字节预算。
    # 函数用途: 计算下一次读取最多还能接收多少字节。
    def available_bytes(self):
        if self._allowance is not None:
            return max(0, self._allowance.max_bytes - self.size)
        cap = self.budget.max_bytes
        if self.request.budget.max_bytes is not None:
            cap = min(cap, self.request.budget.max_bytes)
        return max(0, cap - self.size)

    # LLM: 仅更新本次回执的保护计数，不持久化业务状态。
    # 函数用途: 记录一个被保护或暂缓的候选。
    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    # LLM: 错误列表严格受合同条数上限限制，只接受领域生成的相对路径。
    # 函数用途: 给维护回执补一条有界错误。
    def error(self, code, path):
        if len(self.errors) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
            self.errors.append({"code": code, "path": path})

    # LLM: 返回纯数据副本，不提交维护游标，保存的只有阶段、身份和单个名称续点。
    # 函数用途: 组装本片的统计及下次继续位置。
    def receipt(self, cursor, complete, deferred=""):
        cursor = dict(cursor)
        if self.sweep_state is not None:
            cursor["sweep"] = dict(self.sweep_state)
        else:
            cursor.pop("sweep", None)
        if isinstance(self.retired_probe, dict):
            cursor["retired_probe"] = dict(self.retired_probe)
        else:
            cursor.pop("retired_probe", None)
        result = {
            "cursor": cursor,
            "complete": complete,
            "items": self.items,
            "bytes": self.size,
            "deleted_items": self.deleted,
            "freed_bytes": self.freed,
            "skipped": self.skipped,
            "errors": self.errors,
        }
        if deferred:
            result["deferred_reason"] = deferred
        return result

    # LLM: 预筛在原锁出生前只读执行；只有发现真正到期叶才保留同一窗口交给锁内重新验证。
    # 函数用途: 无到期候选时只推进名称游标，不调用协调器写门。
    def preview(self, path, reachable, started, *, suffix=".json"):
        namespace = str(path.relative_to(self.request.home.owner_home_dir))
        after = (
            self.sweep_state["after"]
            if self.sweep_state and self.sweep_state["namespace"] == namespace
            else ""
        )
        result = _sweep_window(
            path,
            reachable=frozenset(reachable),
            mark_started_at=started,
            now=self.request.now,
            max_items=max(0, self._active_budget().max_items - self.items),
            run_write=self.request.run_write,
            after=after,
            allowed=lambda: not self.exhausted(),
            suffix=suffix,
            dry_run=True,
        )
        if result["eligible"]:
            self.sweep_state = {"namespace": namespace, "after": after}
            return True
        self.items += result["items"]
        self.sweep_state = (
            None if result["complete"] else {"namespace": namespace, "after": result["after"]}
        )
        return False

    # LLM: 消耗标记后剩余预算，只将单叶变更交给原 request.run_write，未追平续点不能重置。
    # 函数用途: 清扫一个命名空间并累计真实删除量。
    def sweep(self, path, reachable, started, *, suffix=".json"):
        namespace = str(path.relative_to(self.request.home.owner_home_dir))
        after = (
            self.sweep_state["after"]
            if self.sweep_state and self.sweep_state["namespace"] == namespace
            else ""
        )
        remaining = max(0, self._active_budget().max_items - self.items)
        result = _sweep_window(
            path,
            reachable=frozenset(reachable),
            mark_started_at=started,
            now=self.request.now,
            max_items=remaining,
            run_write=self.request.run_write,
            after=after,
            allowed=lambda: not self.exhausted(),
            suffix=suffix,
        )
        self.items += result["items"]
        self.deleted += result["deleted"]
        self.freed += result["freed_bytes"]
        self.sweep_state = (
            None if result["complete"] else {"namespace": namespace, "after": result["after"]}
        )
        return result["complete"]


class _MeasuredIndex(ImmutableJsonIndex):
    """保留原 _load 全部验证，只额外计入读取预算。"""

    # LLM: 沿用原不可变索引构造和校验，不注册新持久根。
    # 函数用途: 给原索引附上本片的读取计量器。
    def __init__(self, root, parts, work):
        super().__init__(root, parts)
        self.work = work

    # LLM: 先占用封印长度的预算，再调用原 _load；不放宽摘要、格式及路径验证。
    # 函数用途: 验证一个节点并计算本片读取开销。
    def _load(self, reference):
        try:
            size = reference["ref"]["byte_length"]
            if type(size) is not int or size < 0:
                raise OSError("invalid reference size")
        except (KeyError, TypeError) as exc:
            raise OSError("invalid reference size") from exc
        self.work.read(size)
        return super()._load(reference)


# LLM: 必须先确认所有者相对归属，再完整检查目录链；不存在或链接都不能变为删除权。
# 函数用途: 验证维护路径确实属于当前所有者的真实目录。
def _owner_path(home, path):
    """词法归属加完整父链 no-follow；缺失不等于可删除。"""
    root = Path(home.owner_home_dir)
    path = Path(path)
    relative = path.relative_to(root)
    fs._validate_relative_parts(relative.parts, allow_empty=True)
    with _Directory(path):
        return path


# LLM: 原 no-follow 文件打开负责归属和单链接校验；读取前用剩余字节封顶，按原始返回字节计量。
# 函数用途: 在当前片预算内读取一个文件快照或一条物理行。
def _bounded_read(work, root, parts, *, offset=0, line=False, max_bytes=None):
    work.read(0)
    limit = work.available_bytes() if max_bytes is None else min(max_bytes, work.available_bytes())
    if limit <= 0:
        raise _BudgetExceeded("no read bytes remain")
    descriptor = fs.open_readonly_file_beneath(root, parts)
    with os.fdopen(descriptor, "rb") as handle:
        if not line and os.fstat(handle.fileno()).st_size > limit:
            raise _BudgetExceeded("object exceeds read budget")
        handle.seek(offset)
        raw = handle.readline(limit) if line else handle.read(limit)
    work.size += len(raw)
    if line and len(raw) == limit and not raw.endswith(b"\n"):
        raise _BudgetExceeded("physical journal row exceeds read budget")
    return raw
