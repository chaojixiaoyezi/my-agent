# LLM: 仅维护owner直接子代理投影，不删除locator目标或canonical工作区；活跃/欠账/未知一律保留。
# LLM: 发现与签名共用工作区选路规则，分页游标与累计正事实共用内存生命周期；不改变维护删除协议。
# 模块用途: 显式调用的孤儿删除与七日瘦身、三十日归档服务；不自行启动后台或接入生产清理。
from __future__ import annotations

import errno
import json
import math
import os
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache, partial
from itertools import chain, islice
from pathlib import Path
from typing import Any

from ....common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from ....common.json_io import locked_json_path, write_private_json_file_atomic
from ....common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath
from ....common.opaque_id import validate_opaque_id
from ....io.sqlite_probe import sqlite_readonly_probe_uri
from ....runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from ....runtime_db.schema import runtime_db_path

TERMINAL_STATUSES = frozenset({"DONE", "COMPLETED", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT"})
# 子代理孤儿定位目录被判定可回收前必须等待的宽限秒数。
ORPHAN_GRACE_SECONDS = 3600.0
_SCRATCH = frozenset({"events.jsonl", "heartbeats.jsonl", "heartbeat.json", "heartbeat", "snapshots", "tmp"})
_METADATA = frozenset({"state.json", "workspace_locator.json", "refs.json", "subagent_compact.json"})
_INTERNAL_LOCKS = frozenset(f"{name}.lock" for name in _METADATA)
_SCAN_LOCK = threading.RLock()
_SCANS: OrderedDict[tuple[str, str], tuple[tuple[int, int], Any, bool]] = OrderedDict()
_ADMIN_RECONCILE: ContextVar[tuple | None] = ContextVar("projection_admin_reconcile", default=None)
_MAINTENANCE_BOUNDARY: ContextVar[dict | None] = ContextVar("projection_maintenance_boundary", default=None)
# 唤醒发现实际读取的投影字段；解析缓存只保留这些字段（不存唤醒/孤儿结论，也不存正文其余字段）。
_WAKE_PROJECTION_FIELDS = (
    "runtime_closeout_pending", "run_id", "status", "task_id", "root_id", "root_run_id", "root_task_id",
    "workspace_root", "task_root", "subagent_root", "task_workspace_dir", "agent_run_workspace_dir",
)
# 唤醒发现投影解析记录缓存上限（state.json 与 workspace_locator.json 各占一条）；超出按 LRU 淘汰。
_WAKE_PROJECTION_RECORD_MAX_ENTRIES = 32768
# 指纹未变的解析记录至多复用这么多秒，到期按原路重读核验；与 owner 事实缓存兜底 TTL 同为 10 分钟。
_WAKE_PROJECTION_REVERIFY_SECONDS = 600.0
_WAKE_PROJECTION_RECORDS: OrderedDict[str, tuple[tuple[int, int, int, int, int], float, dict[str, Any]]] = OrderedDict()
_WAKE_PROJECTION_RECORDS_LOCK = threading.Lock()


class ProjectionInspectionBudgetExceeded(ValueError):
    error_code = 'PROJECTION_INSPECTION_BUDGET_EXHAUSTED'


# LLM: 只读nofollow普通文件；缺失可显式为空，损坏、链接和非对象均抛错，不能作删除许可。
# 函数用途: 为发现与维护读取同一投影结构，不写文件或创建目录。
def read_projection_object(path: Path, *, optional: bool = False, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        fd = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        if optional:
            return {}
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        context = _ADMIN_RECONCILE.get()
        if context is not None:
            budget = context[1]
            size = os.fstat(source.fileno()).st_size
            context[2]['bytes'] += size
            if budget is not None and budget.exhausted(size=context[2]['bytes']):
                raise ProjectionInspectionBudgetExceeded('projection inspection byte/deadline budget exhausted')
        if max_bytes is None:
            value = json.load(source)
        else:
            if os.fstat(source.fileno()).st_size > max_bytes:
                raise ProjectionInspectionBudgetExceeded("wake entry grew before reading")
            value = json.loads(source.buffer.read(max_bytes))
    if not isinstance(value, dict):
        raise ValueError("投影必须是JSON对象")
    return value


# LLM: locator优先于payload且字段优先序固定；发现、分类与缓存签名共用选择/校验，不读目标内容也不授删除权。
# 函数用途: 从投影中选出唯一明示绝对工作区路径，未声明时返回None。
def projection_workspace_path(run_dir: Path, payload: dict[str, Any]) -> Path | None:
    locator = read_projection_object(run_dir / "workspace_locator.json", optional=True)
    return select_projection_workspace(locator, payload)


# LLM: 纯选路，不读文件；唤醒签名用本轮已核验的 locator/state 字段调用，维护删除路径仍经上面的现读入口。
# 函数用途: 按 locator 优先、固定字段序选出唯一明示绝对工作区路径，非法值抛错。
def select_projection_workspace(locator: dict[str, Any], payload: dict[str, Any]) -> Path | None:
    for source in (locator, payload):
        for key in ("workspace_root", "task_root", "subagent_root", "task_workspace_dir", "agent_run_workspace_dir"):
            value = source.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("工作区路径无效")
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts or len(path.parts) <= 1:
                raise ValueError("工作区必须是绝对非根路径")
            return path
    return None


# LLM: 只缓存唤醒字段的解析结果，从不缓存唤醒/孤儿/静止结论；closeout、目标存在性与runtime库仍由调用方每轮现判。
#   门槛与 common/cache_freshness 同源：普通文件 + (dev,ino,size,mtime_ns,ctime_ns) 一致 + mtime 已离开粗粒度窗口；
#   读后 lstat 与读前不一致视为不稳定读，只返回本次结果不入缓存；条目超过复核周期必须重读；admin 对账上下文全程绕过。
# 函数用途: 按调用方本轮 lstat 结果取投影唤醒字段；info 为 None 表示文件不存在，返回空对象；读错照常抛给调用方。
def wake_projection_fields(path: Path, info: os.stat_result | None,
                           read: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    if info is None:
        return {}
    key, signature = str(path), cache_stat_signature(info)
    cacheable = (stat.S_ISREG(info.st_mode) and _ADMIN_RECONCILE.get() is None
                 and cache_entry_trustworthy(info.st_mtime_ns))
    if cacheable:
        with _WAKE_PROJECTION_RECORDS_LOCK:
            cached = _WAKE_PROJECTION_RECORDS.get(key)
            if cached is not None and cached[0] == signature and time.monotonic() < cached[1]:
                _WAKE_PROJECTION_RECORDS.move_to_end(key)
                return dict(cached[2])
    payload = read(path)
    fields = {name: payload[name] for name in _WAKE_PROJECTION_FIELDS if name in payload}
    if cacheable:
        try:
            settled = cache_stat_signature(os.lstat(path)) == signature
        except OSError:
            settled = False
        if settled:
            with _WAKE_PROJECTION_RECORDS_LOCK:
                _WAKE_PROJECTION_RECORDS[key] = (signature, time.monotonic() + _WAKE_PROJECTION_REVERIFY_SECONDS, fields)
                _WAKE_PROJECTION_RECORDS.move_to_end(key)
                while len(_WAKE_PROJECTION_RECORDS) > _WAKE_PROJECTION_RECORD_MAX_ENTRIES:
                    _WAKE_PROJECTION_RECORDS.popitem(last=False)
    return dict(fields)


# LLM: 共用选路后仅FileNotFound证明目标消失；权限与解析错误继续向调用方传播，不能被exists吞成孤儿。
# 函数用途: 检查所选locator或旧投影工作区是否确已消失，不读取或删除目标内容。
def projection_workspace_missing(run_dir: Path, payload: dict[str, Any]) -> bool:
    path = projection_workspace_path(run_dir, payload)
    if path is None:
        return False
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


# LLM: 分类只读真实WAL，canonical和存量data库都必须核对；维护新建canonical不能遮蔽旧库活跃事实。
# 函数用途: 返回同run的runtime状态及未消费闹钟；无库或无行不是活跃记录。
def projection_runtime_facts(owner_home: Path, run_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
    statuses, pending = set(), False
    for db in (runtime_db_path(owner_home), owner_home / "data/runtime.db"):
        if not db.exists() and not db.is_symlink():
            continue
        status, debt = _runtime_file_facts(db, run_id, payload)
        if status:
            statuses.add(status)
        pending = pending or debt
    active = statuses - AGENT_RUN_TERMINAL_STATUSES
    return (sorted(active or statuses)[0] if statuses else ""), pending


# LLM: 单库查询绝不初始化schema，拒绝链接，由SQLite管理WAL与锁；任何未知库向调用方报错保留投影。
# 函数用途: 读取一个现存runtime库的运行状态和闹钟。
def _runtime_file_facts(db: Path, run_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
    if db.is_symlink() or db.parent.is_symlink() or not db.is_file():
        raise ValueError("拒绝链接runtime库")
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    if wal.is_symlink() or shm.is_symlink() or (wal.exists() and not shm.is_file()):
        raise ValueError("拒绝链接runtime旁文件")
    uri = sqlite_readonly_probe_uri(db)
    context = _ADMIN_RECONCILE.get()
    if context is not None:
        from ....conversation.runtime_reconciliation import _snapshot
        scope = _snapshot(db)
    else:
        scope = closing(sqlite3.connect(uri, uri=True, timeout=0.05))
    with scope as conn:
        conn.execute("PRAGMA query_only=ON")
        if not conn.in_transaction:
            conn.execute("BEGIN")
        rows = conn.execute("SELECT status FROM agent_runs WHERE run_id = ? OR agent_run_id = ?", (run_id, run_id)).fetchall()
        statuses = {str(row[0]) for row in rows}
        active = statuses - AGENT_RUN_TERMINAL_STATUSES
        status = sorted(active or statuses)[0] if statuses else ""
        # 任务级闹钟可属于整棵树；共享父任务仍欠消费时宁可暂不清理子投影。
        pending = False
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wake_queue'").fetchone():
            ids = (run_id, str(payload.get("root_run_id") or payload.get("root_id") or ""),
                   str(payload.get("task_id") or ""))
            pending = conn.execute(
                "SELECT 1 FROM wake_queue WHERE (root_run_id IN (?, ?, ?) OR root_task_id IN (?, ?, ?)) "
                "AND status NOT IN ('consumed', 'cancelled', 'done') LIMIT 1", (*ids, *ids),
            ).fetchone() is not None
        return status, pending


# LLM: 游标、目录身份和本轮正事实共用一个LRU条目；穷尽、换身份、淘汰或观察异常一起关闭，安静单页不占缓存。
# 函数用途: 在同一锁内枚举并观察至多max_dirs项，返回目录页、穷尽标志和整轮累计正事实；不写磁盘。
def agent_projection_page(owner_home: Path, max_dirs: int, *, purpose: str,
                          observe: Callable[[Path], bool] | None = None) -> tuple[list[Path], bool, bool]:
    root = owner_home / "agents"
    with _SCAN_LOCK:
        key = (str(root), purpose)
        cached = _SCANS.pop(key, None)
        if root.is_symlink() or not root.is_dir() or max_dirs <= 0:
            if cached:
                cached[1].close()
            return [], True, False
        info = root.stat()
        identity = info.st_dev, info.st_ino
        if cached and cached[0] != identity:
            cached[1].close()
            cached = None
        entries = cached[1] if cached else os.scandir(root)
        positive = cached[2] if cached else False
        try:
            batch = list(islice(entries, max_dirs))
            runs = [Path(entry.path) for entry in batch if entry.is_dir(follow_symlinks=False)]
            positive = positive or (observe is not None and any(observe(run) for run in runs))
        except BaseException:
            entries.close()
            raise
        if len(batch) < max_dirs:
            entries.close()
        else:
            _SCANS[key] = (identity, entries, positive)
        while len(_SCANS) > 32:
            _SCANS.popitem(last=False)[1][1].close()
        return runs, len(batch) < max_dirs, positive


# LLM: 调用路径必须是owner/agents的直接实目录；读取整棵投影前拒绝逃逸和链接。
# 函数用途: 验证投影根边界；绝不将locator目标当作清理根。
def _validate_run_dir(owner_home: Path, run_dir: Path) -> None:
    if (owner_home.is_symlink() or (owner_home / "agents").is_symlink() or run_dir.is_symlink()
            or run_dir.parent != owner_home / "agents" or run_dir.name in {"", ".", ".."} or not run_dir.is_dir()):
        raise ValueError("投影目录越界或不是实目录")


# LLM: 清单从固定FD逐段读取，设备与inode一并留证；拒绝链接、特殊文件和多硬链接，不跟随被换掉的路径父链。
# 函数用途: 列出投影相对路径、类型、inode、尺寸和mtime，不读取正文。
def _inventory(run_dir: Path, *, root_fd: int | None = None) -> dict[str, tuple[int, ...]]:
    records = {}
    # LLM: 子目录FD仅在当前递归栈保留，任何读取或身份失败均由finally关闭，不扩大删除授权。
    # 函数用途: 通过已固定的目录逐叶建立清单。
    def walk(parent_fd, prefix):
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                context = _ADMIN_RECONCILE.get()
                if context is not None:
                    context[2]['items'] += 1
                if context is not None and context[1] is not None and context[1].exhausted(context[2]['items']):
                    raise ProjectionInspectionBudgetExceeded('projection inventory budget exhausted')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError("投影包含链接或特殊文件")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ValueError("投影包含多链接文件")
                name = str(Path(prefix) / entry.name)
                records[name] = _record_fp(info)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    try:
                        if _identity(os.fstat(child_fd)) != _identity(info):
                            raise ValueError("清单目录身份已变化")
                        walk(child_fd, name)
                    finally:
                        os.close(child_fd)
    descriptor = os.dup(root_fd) if root_fd is not None else open_directory_beneath(run_dir, (), require_dir_fd=True)
    try:
        walk(descriptor, "")
    finally:
        os.close(descriptor)
    return records


# LLM: 设备号和inode共同构成物理身份，不以路径字符串或目录mtime代替身份。
# 函数用途: 取得目录及普通文件可比较的物理编号。
def _identity(info):
    return info.st_dev, info.st_ino


# LLM: mtime仍占原索引三以保持年龄计算；新增设备号防止跨设备的inode碰撞。
# 函数用途: 为清单和删叶生成统一物理指纹。
def _record_fp(info):
    return info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns, info.st_dev


# LLM: 固定owner及agents根并逐段NOFOLLOW打开run；expected来自分类清单，任一父组件换身份均拒绝。
# 函数用途: 在维护或分类期间持有真实父链FD，结束时归还全部描述符。
@contextmanager
def _pinned_projection(owner_home, run_dir, expected=None):
    with ExitStack() as stack:
        descriptors = {}
        for label, parts in (("owner", ()), ("agents", ("agents",)), ("run", ("agents", run_dir.name))):
            descriptor = open_directory_beneath(owner_home, parts, require_dir_fd=True)
            stack.callback(os.close, descriptor)
            descriptors[label] = descriptor
        ids = {key: _identity(os.fstat(value)) for key, value in descriptors.items()}
        if expected is not None and ids != expected:
            raise ValueError("投影父链与分类快照不一致")
        boundary = {**descriptors, "ids": ids, "owner_home": owner_home, "run_id": run_dir.name,
                    "current_name": run_dir.name}
        _verify_boundary(boundary)
        yield boundary


# LLM: 每次高风险操作重核父链及已固定批次；仅未打开批次的异常恢复可单独核原投影，不授予销毁许可。
# 函数用途: 拒绝owner、agents或当前run目录的身份置换。
def _verify_boundary(boundary, *, include_batch=True):
    for label, parts in (("owner", ()), ("agents", ("agents",)),
                         ("run", ("agents", boundary["current_name"]))):
        descriptor = open_directory_beneath(boundary["owner_home"], parts, require_dir_fd=True)
        try:
            if _identity(os.fstat(descriptor)) != boundary["ids"][label]:
                raise ValueError("维护中的投影父链已变化")
        finally:
            os.close(descriptor)
    if include_batch and boundary.get("batch_name") is not None:
        if boundary.get("batch") is None:
            raise ValueError("整批隔离目录尚未固定FD")
        current = os.stat(boundary["batch_name"], dir_fd=boundary["agents"], follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(boundary["batch"])):
            raise ValueError("整批隔离目录身份已变化")


# LLM: 时间必须有限且结构化；支持已有ISO时间戳，错误不能使目录显得更旧。
# 函数用途: 将投影时间统一为Unix秒供年龄与宽限计算。
def _timestamp(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        result = float(value)
    except (ValueError, TypeError):
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    if not math.isfinite(result):
        raise ValueError("非有限时间")
    return result


# LLM: 明示通知、ack、callback和收口欠账优先保护；不依赖报告自然语言猜状态。
# 函数用途: 从投影及嵌套metadata/attributes识别未消费责任。
def _pending_obligation(payload: dict[str, Any]) -> bool:
    flags = ("runtime_closeout_pending", "parent_wake_pending", "wake_pending", "unread_parent_wake",
             "notification_pending", "ack_pending", "completion_notice_pending", "callback_pending", "delivery_pending")
    if any(payload.get(key) for key in flags):
        return True
    if any(payload.get(key) is False for key in ("parent_notified", "completion_notified", "wake_acknowledged")):
        return True
    return any(_pending_obligation(value) for key in ("attributes", "metadata", "closeout", "notification", "wake")
               if isinstance((value := payload.get(key)), dict))


# LLM: 会话pending队列仍是未读唤醒的事实源；损坏或超预算不能证明已消费。
# 函数用途: 保护关联run/父任务有未读父级唤醒的投影，绝不ack队列。
def _has_parent_wake(
    owner_home: Path, run_id: str, payload: dict[str, Any], *,
    time_expired: Callable[[], bool] | None = None,
    wake_recheck: Callable[[set[str]], set[str] | None] | None = None,
) -> bool:
    ids = _projection_wake_ids(run_id, payload)
    if wake_recheck is not None:
        refs = wake_recheck(ids)
        return refs is None or bool(refs)
    roots = [owner_home / "conversations", owner_home / "data/conversations"]
    if time_expired is not None:
        refs = _collect_active_wake_ids(owner_home, time_expired=time_expired, candidate_ids=ids)
        return refs is None or bool(ids & refs)
    if _ADMIN_RECONCILE.get() is not None:
        refs = _collect_active_wake_ids(owner_home)
        return refs is None or bool(ids & refs)
    roots.extend((owner_home / "workspace/runtime/workspaces").glob("*/conversations"))
    checked = 0
    for root in roots:
        for kind in ("urgent", "normal", "processing", "dedupe"):
            folder = root / "wake_queue" / kind
            if folder.is_symlink():
                return True
            if not folder.is_dir():
                continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json"):
                        continue
                    checked += 1
                    if checked > _UNTIMED_WAKE_SCAN_MAX_ENTRIES:
                        return True
                    wake = read_projection_object(Path(entry.path))
                    if kind == "dedupe" and wake.get("phase") != "prepared":
                        continue
                    if str(wake.get("status", "pending")) in {"consumed", "acked", "acknowledged", "cancelled"}:
                        continue
                    if _references_ids(wake, ids):
                        return True
    return False


# LLM: 仅检查结构化身份字段，正文和摘要绝不参与保护或删除决策。
# 函数用途: 识别嵌套wake source内的run/task引用。
def _references_ids(payload: dict[str, Any], ids: set[str]) -> bool:
    return any((key in {"run_id", "source_run_id", "source_agent_id", "root_run_id", "child_run_id", "task_id", "root_task_id"}
                and isinstance(value, str) and value in ids)
               or (isinstance(value, dict) and _references_ids(value, ids)) for key, value in payload.items())


_WAKE_REF_KEYS = frozenset({
    "run_id",
    "source_run_id",
    "source_agent_id",
    "root_run_id",
    "child_run_id",
    "task_id",
    "root_task_id",
})


def _projection_wake_ids(run_id: str, state_doc: dict[str, Any]) -> set[str]:
    return {
        run_id,
        str(state_doc.get("root_run_id") or state_doc.get("root_id") or ""),
        str(state_doc.get("task_id") or ""),
    } - {""}


def _collect_wake_refs(wake_doc: dict[str, Any], target: set[str]) -> None:
    for key, value in wake_doc.items():
        if key in _WAKE_REF_KEYS and isinstance(value, str) and value:
            target.add(value)
        elif isinstance(value, dict):
            _collect_wake_refs(value, target)


# LLM: 同一维护切片内只扫一次未消费 wake 队列；遇软链接、超预算或坏文件返回 None 以保留全部候选保护。
#   lane 计时调用（time_expired 且非 admin 对账）改走可跨片续扫的持久覆盖；admin 对账与未计时显式调用保持原全量读语义。
# 函数用途: 预先索引活跃唤醒引用的 run/task ID 集合，避免每个投影目录重复全扫 wake_queue。
def _collect_active_wake_ids(
    owner_home: Path, *, time_expired: Callable[[], bool] | None = None, candidate_ids: set[str] | None = None,
) -> set[str] | None:
    roots = (owner_home / "conversations", owner_home / "data/conversations")
    context = _ADMIN_RECONCILE.get()
    if time_expired is not None and context is None:
        return _covered_active_wake_ids(owner_home, time_expired, candidate_ids=candidate_ids)

    def conversation_roots():
        yield from roots
        base = owner_home / "workspace/runtime/workspaces"
        if base.is_symlink():
            raise ValueError("invalid wake workspace directory")
        if not base.is_dir():
            return
        with os.scandir(base) as entries:
            for entry in entries:
                if time_expired is not None and time_expired():
                    raise TimeoutError("wake lookup exceeded lane deadline")
                if context is not None:
                    context[2]["items"] += 1
                    if context[1] is not None and context[1].exhausted(context[2]["items"]):
                        raise TimeoutError("wake workspace discovery exceeded budget")
                if entry.is_symlink():
                    raise ValueError("invalid wake workspace entry")
                if entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path) / "conversations"

    ref_ids: set[str] = set()
    checked = 0
    try:
        with closing(conversation_roots()) as discovered:
            for root in discovered:
                for kind in ("urgent", "normal", "processing", "dedupe"):
                    if time_expired is not None and time_expired():
                        return None
                    folder = root / "wake_queue" / kind
                    if folder.is_symlink():
                        return None
                    if not folder.is_dir():
                        continue
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            if time_expired is not None and time_expired():
                                return None
                            checked += 1
                            if context is not None:
                                context[2]['items'] += 1
                            if context is not None and context[1] is not None and context[1].exhausted(context[2]['items']):
                                return None
                            if checked > _UNTIMED_WAKE_SCAN_MAX_ENTRIES:
                                return None
                            if not entry.name.endswith(".json"):
                                continue
                            wake = read_projection_object(Path(entry.path))
                            if kind == "dedupe" and wake.get("phase") != "prepared":
                                continue
                            if str(wake.get("status", "pending")) in {"consumed", "acked", "acknowledged", "cancelled"}:
                                continue
                            _collect_wake_refs(wake, ref_ids)
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    return ref_ids


# 活跃唤醒引用覆盖：lane 自有的私有 SQLite 旁路状态，只保存唤醒文件 stat 身份与解析出的结构化引用，不存正文。
_WAKE_COVER_PARTS = ("data", "subagent_wake_cover.sqlite3")
_WAKE_QUEUE_KINDS = ("urgent", "normal", "processing", "dedupe")
_WAKE_WORKSPACE_PARTS = ("workspace", "runtime", "workspaces")
_WAKE_DONE_STATUSES = frozenset({"consumed", "acked", "acknowledged", "cancelled"})
# 条目覆盖值：终态无引用；"*" 表示未知/不可读/链接，只要仍在就保护全部候选；工作区目录项记 "dir"。
_WAKE_COVER_TERMINAL = "[]"
_WAKE_COVER_UNKNOWN = "*"
_WAKE_COVER_RETRY = "!"
_WAKE_COVER_DIRECTORY = "dir"
# Derived-store format with retry state and indexed reference membership.
_WAKE_COVER_VERSION = 1
# 未计时显式调用（无 lane 预算）超过这一条数仍按原语义 fail-closed，与 maintain_owner_agent_projections(max_dirs) 默认一致。
_UNTIMED_WAKE_SCAN_MAX_ENTRIES = 5000


# LLM: 与全量读同一判定；dedupe 仅 prepared 为活跃、published 及无 phase 的旧版回执为终态，其余 phase 未知须保护。
# 函数用途: 把一条唤醒文档折算为覆盖值（终态、活跃引用 JSON 列表或未知）。
def _wake_cover_value(wake: dict[str, Any], kind: str) -> str:
    if kind == "dedupe" and "phase" in wake:
        if wake["phase"] == "published":
            return _WAKE_COVER_TERMINAL
        if wake["phase"] != "prepared":
            return _WAKE_COVER_UNKNOWN
    elif kind == "dedupe":
        return _WAKE_COVER_TERMINAL
    if str(wake.get("status", "pending")) in _WAKE_DONE_STATUSES:
        return _WAKE_COVER_TERMINAL
    refs: set[str] = set()
    _collect_wake_refs(wake, refs)
    return json.dumps(sorted(refs))


def _discard_wake_cover(path: Path, opened: os.stat_result) -> None:
    from ....common.nofollow_fs import unlink_file_beneath

    if not os.path.samestat(opened, path.lstat()):
        raise OSError("wake cover changed before rebuild")
    leaves = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal", "")]
    for leaf in leaves:
        try:
            info = leaf.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("invalid wake cover file during rebuild")
    for leaf in leaves:
        unlink_file_beneath(path.parent, (leaf.name,))


def _initialize_wake_cover(conn) -> None:
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(f"""
        BEGIN IMMEDIATE;
        CREATE TABLE wake_cover_folders(folder TEXT PRIMARY KEY, identity TEXT NOT NULL,
            cursor TEXT, sweep INTEGER NOT NULL, start_sig TEXT NOT NULL, covered_sig TEXT NOT NULL,
            covered_at REAL NOT NULL);
        CREATE TABLE wake_cover_entries(folder TEXT NOT NULL, name TEXT NOT NULL,
            sig TEXT NOT NULL, sweep INTEGER NOT NULL, refs TEXT, PRIMARY KEY(folder, name)) WITHOUT ROWID;
        CREATE INDEX wake_cover_pending ON wake_cover_entries(folder, sweep, name)
            WHERE refs IS NULL OR refs = '!';
        CREATE INDEX wake_cover_unverified ON wake_cover_entries(folder)
            WHERE refs IS NULL OR refs IN ('*', '!');
        CREATE INDEX wake_cover_workspaces ON wake_cover_entries(folder, name) WHERE refs = 'dir';
        CREATE TABLE wake_cover_refs(folder TEXT NOT NULL, name TEXT NOT NULL, ref TEXT NOT NULL,
            PRIMARY KEY(folder, name, ref), FOREIGN KEY(folder, name)
            REFERENCES wake_cover_entries(folder, name) ON DELETE CASCADE) WITHOUT ROWID;
        CREATE INDEX wake_cover_ref_lookup ON wake_cover_refs(ref);
        PRAGMA user_version={_WAKE_COVER_VERSION};
        COMMIT;
    """)


@contextmanager
def _wake_cover_connection(owner_home: Path):
    from ....common.nofollow_fs import ensure_private_dir
    from ....io.sqlite_connection import sqlite_error_details
    from ....io.sqlite_scope import ensure_private_sqlite_birth

    path = owner_home.joinpath(*_WAKE_COVER_PARTS)
    ensure_private_dir(path.parent)
    # All cover connections share the stable owner lock, including generation replacement.
    with locked_json_path(path, blocking=False):
        for allow_upgrade in (True, False):
            created = ensure_private_sqlite_birth(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("invalid wake cover store")
            try:
                with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0,
                                             isolation_level=None)) as conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA journal_size_limit=1048576")
                    if created:
                        _initialize_wake_cover(conn)
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version == _WAKE_COVER_VERSION:
                        yield conn
                        return
                    if version != 0 or not allow_upgrade:
                        raise ValueError("unsupported wake cover version")
                    # A busy legacy connection keeps its current generation.
                    conn.execute("BEGIN EXCLUSIVE")
                    conn.rollback()
                    if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        raise BlockingIOError("legacy wake cover journal is busy")
            except sqlite3.Error as error:
                if sqlite_error_details(error)["sqlite_primary_code"] in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                    _discard_wake_cover(path, info)
                raise
            _discard_wake_cover(path, info)


def _forget_wake_folder(conn, folder: str) -> None:
    conn.execute("DELETE FROM wake_cover_entries WHERE folder=?", (folder,))
    conn.execute("DELETE FROM wake_cover_folders WHERE folder=?", (folder,))


def _wake_entry_signature(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


# LLM: 只 lstat，不读正文；签名 (st_ino, st_size, st_mtime_ns) 未变的条目沿用原覆盖值，变化或新条目置待读。
# 函数用途: 把目录分页中的一个名字登记进本轮覆盖。
def _list_wake_entry(conn, directory: Path, folder: str, name: str, kind: str, sweep: int) -> None:
    if kind != "workspaces" and not name.endswith(".json"):
        return
    try:
        info = os.lstat(directory / name)
    except FileNotFoundError:
        return
    except OSError:
        conn.execute("INSERT OR REPLACE INTO wake_cover_entries VALUES (?,?,?,?,?)",
                     (folder, name, "", sweep, _WAKE_COVER_UNKNOWN if kind == "workspaces" else _WAKE_COVER_RETRY))
        return
    if kind == "workspaces":
        sig = str(info.st_ino)
        value = (_WAKE_COVER_UNKNOWN if stat.S_ISLNK(info.st_mode) else
                 _WAKE_COVER_DIRECTORY if stat.S_ISDIR(info.st_mode) else _WAKE_COVER_TERMINAL)
    else:
        sig = _wake_entry_signature(info)
        value = None if stat.S_ISREG(info.st_mode) else _WAKE_COVER_UNKNOWN
    row = conn.execute("SELECT sig FROM wake_cover_entries WHERE folder=? AND name=?", (folder, name)).fetchone()
    if row is not None and row[0] == sig:
        conn.execute("UPDATE wake_cover_entries SET sweep=? WHERE folder=? AND name=?", (sweep, folder, name))
    else:
        conn.execute("INSERT OR REPLACE INTO wake_cover_entries VALUES (?,?,?,?,?)", (folder, name, sig, sweep, value))


# LLM: 读前后签名一致才记录结果；读中被替换则留待读，绝不把半途结果当覆盖；缺失即删条目。
# 函数用途: 读取一个新增或变化的唤醒文件并写入覆盖值；返回 False 表示仍待读。
def _read_wake_entry(conn, directory: Path, folder: str, name: str, kind: str, work) -> bool:
    path = directory / name
    try:
        before = os.lstat(path)
        value = _WAKE_COVER_UNKNOWN
        if stat.S_ISREG(before.st_mode):
            size = before.st_size
            if work.budget is not None and size > work.budget.max_bytes:
                value = _WAKE_COVER_RETRY
            elif work.budget is not None and size > work.budget.max_bytes - work.usage["bytes"]:
                return False
            else:
                work.usage["bytes"] += size
                try:
                    value = _wake_cover_value(read_projection_object(path, max_bytes=size), kind)
                except FileNotFoundError:
                    raise
                except (OSError, ProjectionInspectionBudgetExceeded):
                    value = _WAKE_COVER_RETRY
                except (ValueError, TypeError, RecursionError):
                    value = _WAKE_COVER_UNKNOWN
        after = os.lstat(path)
    except FileNotFoundError:
        conn.execute("DELETE FROM wake_cover_entries WHERE folder=? AND name=?", (folder, name))
        return True
    except OSError:
        conn.execute("UPDATE wake_cover_entries SET refs=? WHERE folder=? AND name=?",
                     (_WAKE_COVER_RETRY, folder, name))
        return True
    signature = _wake_entry_signature(before)
    if signature != _wake_entry_signature(after):
        value = None
    conn.execute("UPDATE wake_cover_entries SET sig=?, refs=? WHERE folder=? AND name=?", (signature, value, folder, name))
    if value not in (None, _WAKE_COVER_UNKNOWN, _WAKE_COVER_RETRY, _WAKE_COVER_TERMINAL):
        conn.executemany("INSERT INTO wake_cover_refs VALUES (?,?,?)",
                         ((folder, name, ref) for ref in json.loads(value)))
    return True


class _WakeCoverWork:
    def __init__(self, time_expired, budget, usage, visited_entries):
        self.time_expired = time_expired
        self.budget = budget
        self.usage = usage
        self.visited_entries = visited_entries
        self.started = False

    def capacity(self):
        from ....user_space.storage_lanes import STORAGE_LANE_SLICE_ENTRIES

        expired = self.time_expired()
        if self.started and expired:
            return 0
        limit = STORAGE_LANE_SLICE_ENTRIES
        if self.budget is not None:
            limit = min(limit, self.budget.max_items - self.usage["items"] - self.visited_entries)
        # Even if opening the cover used the time allowance, one useful unit can make progress.
        return max(0, min(limit, 1) if expired else limit)

    def charge(self, count):
        self.started = True
        self.usage["items"] += count


def _advance_wake_folder(conn, owner_home: Path, parts: tuple[str, ...], kind: str,
                         work: _WakeCoverWork, strict: bool) -> str:
    from ....common.cache_freshness import cache_entry_trustworthy
    from ....user_space.directory_paging import directory_page

    folder, directory = "/".join(parts), owner_home.joinpath(*parts)
    row = conn.execute("SELECT identity, cursor, sweep, start_sig, covered_sig, covered_at FROM wake_cover_folders "
                       "WHERE folder=?", (folder,)).fetchone()
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        if row is not None:
            _forget_wake_folder(conn, folder)
        return "covered"
    if not stat.S_ISDIR(info.st_mode):
        return "protect"
    identity = f"{info.st_dev}:{info.st_ino}"
    signature = f"{identity}:{info.st_mtime_ns}"
    if row is not None and row[0] != identity:
        _forget_wake_folder(conn, folder)
        row = None
    cursor, sweep, start_sig, covered_sig, covered_at = (
        (None, 0, "", "", 0.0) if row is None else (json.loads(row[1]) if row[1] else None, *row[2:]))
    if cursor is None and covered_sig == signature and 0 <= time.time() - covered_at < _WAKE_PROJECTION_REVERIFY_SECONDS:
        return "covered"
    outcome = "pending"
    recheck_changed = True
    while limit := work.capacity():
        if cursor is None:
            # 起扫时目录 mtime 仍在粗粒度窗口内的，同一时间片内的后续修改可能不改 mtime，故不能持久复用。
            sweep, start_sig, covered_sig = sweep + 1, signature, ""
            cursor = {"page": {}, "durable": cache_entry_trustworthy(info.st_mtime_ns)}
        if "page" in cursor:
            names, resume, complete = directory_page(owner_home, parts, cursor["page"], limit, require_dir_fd=strict)
            work.charge(resume.get("last_scanned", len(names)))
            for name in names:
                _list_wake_entry(conn, directory, folder, name, kind, sweep)
            cursor = {"durable": cursor["durable"], **({} if complete else {"page": resume})}
            continue
        # Persist a keyset for both virgin and retryable entries. A failed read cannot hide later entries.
        pending = conn.execute("SELECT name FROM wake_cover_entries INDEXED BY wake_cover_pending "
                               "WHERE folder=? AND sweep=? AND name>? "
                               "AND (refs IS NULL OR refs = '!') ORDER BY name LIMIT ?",
                               (folder, sweep, cursor.get("read_after", ""), limit)).fetchall()
        if pending:
            deferred = False
            for (name,) in pending:
                if not work.capacity():
                    deferred = True
                    break
                work.charge(1)
                if not _read_wake_entry(conn, directory, folder, name, kind, work):
                    deferred = True
                    break
                cursor["read_after"] = name
            if deferred:
                break
            continue
        outstanding = conn.execute("SELECT refs FROM wake_cover_entries INDEXED BY wake_cover_pending "
                                   "WHERE folder=? AND sweep=? "
                                   "AND (refs IS NULL OR refs = '!') LIMIT 1",
                                   (folder, sweep)).fetchone()
        if outstanding is not None:
            cursor.pop("read_after", None)
            if outstanding[0] == _WAKE_COVER_RETRY or not recheck_changed:
                # Recheck transient failures on the next slice, after this pass has visited every pending entry.
                break
            recheck_changed = False
            continue
        if kind == "workspaces":
            for (name,) in conn.execute("SELECT name FROM wake_cover_entries WHERE folder=? AND sweep!=? AND refs=?",
                                        (folder, sweep, _WAKE_COVER_DIRECTORY)):
                for queue in _WAKE_QUEUE_KINDS:
                    _forget_wake_folder(conn, "/".join((*parts, name, "conversations", "wake_queue", queue)))
        conn.execute("DELETE FROM wake_cover_entries WHERE folder=? AND sweep!=?", (folder, sweep))
        latest = os.lstat(directory)
        if f"{latest.st_dev}:{latest.st_ino}" != identity:
            raise ValueError("wake queue directory replaced during cover sweep")
        durable, cursor = cursor["durable"], None
        if f"{identity}:{latest.st_mtime_ns}" == start_sig:
            outcome = "covered"
            if durable:
                covered_sig, covered_at = start_sig, time.time()
            break
        # 扫描期间目录有变化或有条目读中被替换（留待读）：按最新签名在剩余预算内重扫（未变条目仅 lstat）。
        info, signature = latest, f"{identity}:{latest.st_mtime_ns}"
    conn.execute("INSERT OR REPLACE INTO wake_cover_folders VALUES (?,?,?,?,?,?,?)",
                 (folder, identity, None if cursor is None else json.dumps(cursor), sweep, start_sig, covered_sig,
                  covered_at))
    return outcome


# LLM: 跨片续扫的活跃唤醒引用覆盖：每片只重读新增或变化条目，只有全部文件夹对当前身份完成覆盖才返回集合；
#   链接、非目录、不可读、未知 phase 与未完成覆盖一律返回 None（保护全部候选）；每次调用至少推进一个单元以保证极短片仍有进展。
# 函数用途: 为 lane 计时调用提供不随唤醒文件总数增长而失效的引用集合。
def _covered_active_wake_ids(owner_home: Path, time_expired: Callable[[], bool], *,
                             budget=None, usage=None, visited_entries=0,
                             candidate_ids: set[str] | None = None) -> set[str] | None:
    from ....user_space.directory_paging import _native_directory_supported

    work = _WakeCoverWork(time_expired, budget, usage if usage is not None else {"items": 0, "bytes": 0},
                          visited_entries)
    strict = _native_directory_supported()
    try:
        with _wake_cover_connection(owner_home) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                refs = _read_wake_cover(conn, owner_home, work, strict, candidate_ids)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            # 有界回收已删条目的空闲页；pragma 逐步执行，必须取尽结果才真正完成。
            conn.execute("PRAGMA incremental_vacuum(64)").fetchall()
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error):
        return None
    return refs


def _read_wake_cover(conn, owner_home, work, strict, candidate_ids):
    def covered(parts, kind):
        if _advance_wake_folder(conn, owner_home, parts, kind, work, strict) != "covered":
            return False
        return conn.execute("SELECT 1 FROM wake_cover_entries INDEXED BY wake_cover_unverified WHERE folder=? "
                            "AND (refs IS NULL OR refs IN ('*', '!')) LIMIT 1", ("/".join(parts),)).fetchone() is None

    if not covered(_WAKE_WORKSPACE_PARTS, "workspaces"):
        return None
    with closing(conn.execute("SELECT name FROM wake_cover_entries WHERE folder=? AND refs='dir' ORDER BY name",
                              ("/".join(_WAKE_WORKSPACE_PARTS),))) as rows:
        workspaces = ((*_WAKE_WORKSPACE_PARTS, name, "conversations") for (name,) in rows)
        for root in chain((("conversations",), ("data", "conversations")), workspaces):
            for kind in _WAKE_QUEUE_KINDS:
                if not covered((*root, "wake_queue", kind), kind):
                    return None
    if candidate_ids is None:
        return {ref for (ref,) in conn.execute("SELECT DISTINCT ref FROM wake_cover_refs")}
    return {ref for ref in candidate_ids if conn.execute(
        "SELECT 1 FROM wake_cover_refs WHERE ref=? LIMIT 1", (ref,)).fetchone() is not None}


def _has_active_parent_wake(
    owner_home: Path,
    run_id: str,
    state_doc: dict[str, Any],
    *,
    wake_lookup: Callable[[], set[str] | None] | None = None,
    wake_ref_ids: set[str] | None = None,
) -> bool:
    if wake_ref_ids is not None:
        return bool(_projection_wake_ids(run_id, state_doc) & wake_ref_ids)
    if wake_lookup is None:
        return _has_parent_wake(owner_home, run_id, state_doc)
    ref_ids = wake_lookup()
    if ref_ids is None:
        return True
    return bool(_projection_wake_ids(run_id, state_doc) & ref_ids)


# LLM: 只收集结构化产物路径，不读取正文；保留旧投影refs及既有compact中的信息。
# 函数用途: 汇合报告、输出和交付物引用，供compact保真与唯一产物保护。
def _deliverables(payload: dict[str, Any]) -> list[str]:
    result = []
    for key in ("deliverable_paths", "artifact_refs", "declared_output_refs", "output_files", "final_report_ref",
                "final_report", "output_json_ref", "output_json", "runner_result_ref", "result_file_ref", "result_file"):
        value = payload.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = item.get("path") or item.get("ref")
            if isinstance(item, str) and item.strip() and item not in result:
                result.append(item)
    return result


# LLM: 引用规范化仅能增加保护，绝不扩大删除根；含..、目录自身或无法钉到清单的相对引用保留整份投影。
# 函数用途: 解析唯一交付物位置，保护实际子目录并对歧义引用停止全部scratch删除。
def _protected_files(run_dir: Path, payload: dict[str, Any], records: dict) -> tuple[set[str], bool]:
    roots = set()
    all_roots = {Path(name).parts[0] for name in records}
    canonical_run = run_dir.resolve(strict=True)
    for value in _deliverables(payload):
        path = Path(value)
        if ".." in path.parts or "\x00" in value:
            return all_roots, True
        relative_input = not path.is_absolute()
        if relative_input:
            path = run_dir / path
        try:
            relative = path.resolve(strict=False).relative_to(canonical_run)
        except ValueError:
            if relative_input:
                return all_roots, True
            continue
        if not relative.parts or (relative_input and str(relative) not in records):
            return all_roots, True
        roots.add(relative.parts[0])
    unknown = any(Path(name).parts[0] not in _SCRATCH | _METADATA | _INTERNAL_LOCKS for name in records)
    return roots, bool(roots or unknown)


# LLM: 分类同时固定物理父链和FD清单供执行复核；先做状态、年龄与工作区存在性便宜筛查，候选目录才探测运行库和唤醒队列。
# 函数用途: 读取一致清单并给出清理候选，返回内部证据供执行前复核。
def _classify(owner_home, run_dir, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days,
              wake_lookup: Callable[[], set[str] | None] | None = None,
              wake_ref_ids: set[str] | None = None):
    result = {"run_id": run_dir.name, "classification": "active_or_protected", "reason": "unknown", "age_seconds": 0.0}
    try:
        _validate_run_dir(owner_home, run_dir)
        payload = read_projection_object(run_dir / "state.json")
        if payload.get("run_id", run_dir.name) != run_dir.name:
            raise ValueError("投影run_id与目录冲突")
        with _pinned_projection(owner_home, run_dir) as boundary:
            records = _inventory(run_dir, root_fd=boundary["run"])
            result.update(payload=payload, inventory=records, directory_ids=boundary["ids"])
        if (run_dir.name in (active_run_ids or ()) or run_dir.name in (pinned_run_ids or ())
                or _pending_obligation(payload)):
            result["reason"] = "explicit_protection"
            return result
        refs = read_projection_object(run_dir / "refs.json", optional=True)
        previous = read_projection_object(run_dir / "subagent_compact.json", optional=True)
        evidence = {**previous, **refs, **payload}
        evidence["deliverable_paths"] = list(dict.fromkeys(_deliverables(previous) + _deliverables(refs) + _deliverables(payload)))
        result["compact_source"] = evidence
        protected_roots, unique = _protected_files(run_dir, evidence, records)
        result["protected_roots"] = protected_roots
        modified = [fp[3] / 1e9 for name, fp in records.items()
                    if Path(name).parts[0] != "subagent_compact.json" and name not in _INTERNAL_LOCKS]
        age = max(0.0, now - max([_timestamp(payload.get("updated_at")), *modified], default=run_dir.stat().st_mtime))
        result["age_seconds"] = age
        scratch = {Path(name).parts[0] for name in records} & _SCRATCH - protected_roots
        result["scratch_roots"] = scratch
        state_doc = result["pay" + "load"]
        workspace_missing = (
            age >= ORPHAN_GRACE_SECONDS
            and not unique
            and projection_workspace_missing(run_dir, state_doc)
        )
        context = _ADMIN_RECONCILE.get()
        if context is not None:
            agent, budget = context[:2]
            from ....conversation.runtime_reconciliation import inspect_parent_terminal
            owner = state_doc.get('owner', state_doc.get('owner_id'))
            root_id = str(state_doc.get('root_run_id') or state_doc.get('root_id') or '')
            if (owner != agent.home_paths.owner_id or not root_id or unique or age < ORPHAN_GRACE_SECONDS
                    or state_doc.get('runner_active_attempt_id') or state_doc.get('runner_pid') or state_doc.get('pid')
                    or any(name in records for name in ('heartbeat.json', 'heartbeat'))
                    or state_doc.get('input_dependency_refs') or state_doc.get('pending_inputs')
                    or any(request.get('status') not in {'CLOSED', 'DENIED', 'CANCELLED'}
                        for request in state_doc.get('capability_requests', ()) if isinstance(request, dict))):
                result['reason'] = 'authority_missing_reference_unproven'
                return result
            if not any(path.is_file() for path in (runtime_db_path(owner_home), owner_home / 'data/runtime.db')):
                result['reason'] = 'authority_missing_coverage_unknown'
                return result
            runtime_status, pending = projection_runtime_facts(owner_home, run_dir.name, state_doc)
            if runtime_status or pending or not inspect_parent_terminal(agent, root_id)['eligible']:
                result['reason'] = 'authority_present_or_parent_unproven'
                return result
            if _has_active_parent_wake(owner_home, run_dir.name, state_doc, wake_lookup=wake_lookup, wake_ref_ids=wake_ref_ids):
                result['reason'] = 'wake_reference_unknown_or_active'
                return result
            result.update(classification='authority_missing_archive_candidate', reason='authority_missing')
            result['compact_source'] = {**evidence, 'authority_state': 'authority_missing'}
            return result
        status_upper = str(state_doc.get("status", "")).upper()
        archive_eligible = False
        slim_eligible = False
        if not workspace_missing:
            if status_upper not in TERMINAL_STATUSES:
                result["reason"] = "nonterminal_or_unknown"
                return result
            archive_eligible = age >= archive_after_days * 86400 and not unique
            slim_eligible = age >= slim_after_days * 86400 and bool(scratch)
            if not archive_eligible and not slim_eligible:
                if "subagent_compact.json" in records and not scratch:
                    result.update(classification="terminal_compact_stub_only", reason="already_slim")
                else:
                    result["reason"] = "young_or_unique"
                return result
        runtime_status, pending = projection_runtime_facts(owner_home, run_dir.name, state_doc)
        if (runtime_status and runtime_status not in AGENT_RUN_TERMINAL_STATUSES) or pending or _has_active_parent_wake(
            owner_home, run_dir.name, state_doc, wake_lookup=wake_lookup, wake_ref_ids=wake_ref_ids
        ):
            result["reason"] = "runtime_or_wake_active"
            return result
        if workspace_missing:
            result.update(classification="dangling_orphan", reason="workspace_missing")
            return result
        if archive_eligible:
            result.update(classification="terminal_archive_candidate", reason="archive_age")
        elif slim_eligible:
            result.update(classification="terminal_slim_candidate", reason="slim_age")
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError) as exc:
        result["reason"] = getattr(exc, "error_code", f"unreadable:{type(exc).__name__}")
    return result


# LLM: 公共只读分类默认七/三十日；无副作用，损坏或不安全目录一律返回保护而非推测可删。
# 函数用途: 给调用方和dry-run查看单个投影当前属于哪类。
def classify_agent_projection_dir(owner_home: Path, run_dir: Path, *, now: float | None = None,
                                  active_run_ids: set[str] | None = None, pinned_run_ids: set[str] | None = None) -> dict[str, Any]:
    info = _classify(Path(owner_home), Path(run_dir), now=time.time() if now is None else float(now),
                     active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                     slim_after_days=7.0, archive_after_days=30.0)
    return {key: value for key, value in info.items() if key in {"run_id", "classification", "reason", "age_seconds"}}


# LLM: 使用原字段保留结果，既有compact能补足轻量state；不改变canonical状态或伪造运行统计。
# 函数用途: 构造归档与瘦身共用的稳定摘要结构。
def _compact(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {"schema": "subagent_compact.v1", "run_id": run_id,
            "parent_run_id": payload.get("parent_run_id", payload.get("parent_id", "")),
            "root_run_id": payload.get("root_run_id", payload.get("root_id", "")),
            "task_id": payload.get("task_id", ""), "status": payload.get("status", ""),
            **({"authority_state": "authority_missing"} if payload.get("authority_state") == "authority_missing" else {}),
            "created_at": payload.get("created_at"), "updated_at": payload.get("updated_at"),
            "result_summary": payload.get("result_summary", payload.get("last_progress_summary", "")),
            "deliverable_paths": _deliverables(payload), "error_summary": payload.get("error_summary", payload.get("error", "")),
            "usage_summary": payload.get("usage_summary", payload.get("usage", {}))}


# LLM: 归档在固定agents FD内NOFOLLOW打开，持同一普通文件FD锁完成去重、追加、同步及读回；路径变更拒绝删除原件。
# 函数用途: 耐久保存一个终态摘要，成功前调用方不能删除投影。
def _archive_entry(path: Path, compact: dict[str, Any]) -> None:
    import fcntl

    boundary = _MAINTENANCE_BOUNDARY.get()
    if boundary is None:
        raise ValueError("归档缺少受信维护边界")
    parts = path.parent.relative_to(boundary['owner_home']).parts
    if parts != ('agents',) and not (_ADMIN_RECONCILE.get() is not None and len(parts) == 3
            and parts[:2] == ('agents', 'archived_subagents') and path.name == boundary['run_id'] + '.jsonl'):
        raise ValueError("归档缺少受信维护边界")
    if (not isinstance(compact, dict) or compact.get("schema") != "subagent_compact.v1"
            or compact.get("run_id") != boundary["run_id"]):
        raise ValueError("归档新条目schema或run_id不符")
    validate_opaque_id(compact["run_id"], kind="run_id")
    _verify_boundary(boundary)
    with ExitStack() as stack:
        if parts != ('agents',):
            archive_fd = open_directory_beneath(boundary['owner_home'], parts, create=True, require_dir_fd=True)
            stack.callback(os.close, archive_fd)
            boundary['archive_fd'], boundary['archive_parts'] = archive_fd, parts
            stack.callback(lambda: boundary.pop('archive_fd', None))
            stack.callback(lambda: boundary.pop('archive_parts', None))
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600, dir_fd=boundary.get('archive_fd', boundary["agents"]))
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _verify_archive_leaf(boundary, path.name, fd)
        target = stack.enter_context(os.fdopen(os.dup(fd), "a+", encoding="utf-8"))
        target.seek(0)
        context = _ADMIN_RECONCILE.get()
        if context is not None and context[1] is not None and context[1].exhausted(size=os.fstat(fd).st_size):
            raise ProjectionInspectionBudgetExceeded('archive receipt exceeds inspection budget')
        found = False
        for line in target:
            if not line.endswith("\n"):
                raise ValueError("归档缺少末尾换行")
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("schema") != "subagent_compact.v1" or not entry.get("run_id"):
                raise ValueError("归档条目损坏")
            if entry["run_id"] == compact["run_id"]:
                if entry != compact or found:
                    raise ValueError("同run归档内容冲突或重复")
                found = True
        _verify_archive_leaf(boundary, path.name, fd)
        os.fchmod(fd, 0o600)
        if not found:
            target.write(json.dumps(compact, ensure_ascii=False, sort_keys=True) + "\n")
        target.flush()
        os.fsync(fd)
        target.seek(0)
        if not any(json.loads(line) == compact for line in target):
            raise ValueError("归档读回失败")
        _verify_archive_leaf(boundary, path.name, fd)
        os.fsync(boundary.get("archive_fd", boundary["agents"]))


# LLM: FD仍可读不代表路径仍指向该归档；单链接、普通类型和当前路径身份同时成立才准继续。
# 函数用途: 核对归档打开前后未被换名、链接或脱链。
def _verify_archive_leaf(boundary, name, fd):
    _verify_boundary(boundary)
    if 'archive_parts' in boundary:
        descriptor = open_directory_beneath(boundary['owner_home'], boundary['archive_parts'], require_dir_fd=True)
        try:
            if _identity(os.fstat(descriptor)) != _identity(os.fstat(boundary['archive_fd'])):
                raise ValueError('archive bucket identity changed')
        finally:
            os.close(descriptor)
    info = os.fstat(fd)
    current = os.stat(name, dir_fd=boundary.get('archive_fd', boundary["agents"]), follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1 or _identity(info) != _identity(current)):
        raise ValueError("归档不是当前单链接普通文件")


# LLM: owner目录EX与真实发布的祖先SH共用同一inode；固定父链后初始化canonical并锁runtime写事务及state，锁序先目录后业务锁。
# 函数用途: 为末次验证到删除持有真实FD与运行库锁；不改业务行，锁忙放弃本目录。
@contextmanager
def _maintenance_guard(owner_home: Path, run_dir: Path, *, expected=None):
    import fcntl

    from ....runtime_db.repository import RuntimeRepository

    with _pinned_projection(owner_home, run_dir, expected) as boundary, ExitStack() as stack:
        fcntl.flock(boundary["owner"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        canonical = runtime_db_path(owner_home)
        # 显式归档只锁现存权威库，不创建空 canonical 旁路。
        if _ADMIN_RECONCILE.get() is None:
            try:
                fd = os.open(canonical.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=boundary["owner"])
            except FileExistsError:
                pass
            else:
                os.close(fd)
                RuntimeRepository(canonical)
        for db in (canonical, owner_home / "data/runtime.db"):
            if not db.exists() and not db.is_symlink():
                continue
            if db.is_symlink() or db.parent.is_symlink() or not db.is_file():
                raise ValueError("拒绝链接runtime库")
            conn = stack.enter_context(closing(sqlite3.connect(db.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.05)))
            conn.execute("BEGIN IMMEDIATE")
            stack.callback(conn.rollback)
        if (run_dir / "state.json.lock").exists():
            fd = os.open("state.json.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=boundary["run"])
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("state锁不是单链接普通文件")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = _MAINTENANCE_BOUNDARY.set(boundary)
        try:
            yield boundary
        finally:
            _MAINTENANCE_BOUNDARY.reset(token)


# LLM: compact是本次唯一允许新增/替换的叶；原state/refs/locator及其余清单变化均不得成为新删除授权。
# 函数用途: 检查写compact后的非服务输入仍与分类时一致，并重查欠账保护。
def _verify_before_delete(owner_home, run_dir, info, *, now, active_run_ids, pinned_run_ids, slim_after_days,
                          archive_after_days, time_expired=None, wake_recheck=None):
    ids = _projection_wake_ids(run_dir.name, info["payload"])
    wake_lookup = (partial(wake_recheck, ids) if wake_recheck is not None else
                   partial(_collect_active_wake_ids, owner_home, time_expired=time_expired, candidate_ids=ids)
                   if time_expired is not None else None)
    fresh = _classify(owner_home, run_dir, now=now, active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                      slim_after_days=slim_after_days, archive_after_days=archive_after_days,
                      wake_lookup=wake_lookup)
    if time_expired is not None and time_expired():
        raise TimeoutError("projection verification exceeded lane deadline")
    if fresh["classification"] != info["classification"]:
        raise ValueError("删除前运行已恢复或出现欠账")
    excluded = {"subagent_compact.json", "subagent_compact.json.lock"}
    before = {key: value for key, value in info["inventory"].items() if key not in excluded}
    after = {key: value for key, value in fresh["inventory"].items() if key not in excluded}
    if before != after or fresh["payload"] != info["payload"]:
        raise ValueError("删除前投影输入已变化")
    return fresh["inventory"]


# LLM: 执行沿分类时父链身份取锁；末次权威核验到删叶共用FD临界区，归档失败不删原件。
# 函数用途: 执行一个候选目录的完整持锁维护动作。
def _maintain_projection(owner_home, run_dir, info, report, *, now, active_run_ids, pinned_run_ids, slim_after_days,
                         archive_after_days, time_expired=None, wake_recheck=None):
    options = dict(now=now, active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                   slim_after_days=slim_after_days, archive_after_days=archive_after_days, time_expired=time_expired,
                   wake_recheck=wake_recheck)
    with _maintenance_guard(owner_home, run_dir, expected=info["directory_ids"]) as boundary:
        boundary["payload"] = info["payload"]
        boundary["time_expired"] = time_expired
        boundary["wake_recheck"] = wake_recheck
        records = _verify_before_delete(owner_home, run_dir, info, **options)
        kind = info["classification"]
        if kind == "terminal_slim_candidate":
            compact = _compact(info["compact_source"], run_dir.name)
            write_private_json_file_atomic(run_dir / "subagent_compact.json", compact)
            if read_projection_object(run_dir / "subagent_compact.json") != compact:
                raise ValueError("compact读回失败")
            records = _verify_before_delete(owner_home, run_dir, info, **options)
            _remove_records(run_dir, records, info["scratch_roots"], report)
            report["slimmed_dirs"] += 1
        else:
            if kind in {"terminal_archive_candidate", "authority_missing_archive_candidate"}:
                archive = owner_home / "agents" / datetime.fromtimestamp(now, timezone.utc).strftime("archived_subagents_%Y_%m.jsonl")
                if kind == 'authority_missing_archive_candidate':
                    day = datetime.fromtimestamp(_timestamp(info['payload'].get('updated_at')), timezone.utc).strftime('%Y-%m-%d')
                    archive = owner_home / 'agents' / 'archived_subagents' / day / (run_dir.name + '.jsonl')
                _archive_entry(archive, _compact(info["compact_source"], run_dir.name))
                records = _verify_before_delete(owner_home, run_dir, info, **options)
            _remove_records(run_dir, records, None, report)
            if kind in {"terminal_archive_candidate", "authority_missing_archive_candidate"}:
                report["archived_dirs"] += 1


# LLM: 空库存不是删除授权；非空投影先无覆盖隔离，整批待删根校验后才unlink；恢复冲突和部分I/O显式留证。
# 函数用途: 分离元数据与整批待删项，在共享维护锁内删除并按实际结果恢复名称。
def _remove_records(run_dir: Path, records: dict, roots: set[str] | None, report: dict) -> None:
    boundary = _MAINTENANCE_BOUNDARY.get()
    if boundary is None or run_dir != boundary["owner_home"] / "agents" / boundary["run_id"]:
        raise ValueError("删除缺少受信维护边界")
    if not records:
        raise ValueError("空库存不能授权删除投影")
    _verify_boundary(boundary)
    if _inventory(run_dir, root_fd=boundary["run"]) != records:
        raise ValueError("删除前投影已变化")
    selected = [name for name in records if roots is None or Path(name).parts[0] in roots]
    tombstone = f".purge_{run_dir.name}_{uuid.uuid4().hex}"
    _rename_noreplace(boundary["agents"], run_dir.name, boundary["agents"], tombstone)
    boundary["current_name"] = tombstone
    boundary["deleted_entries"] = 0
    with ExitStack() as stack:
        try:
            _assert_no_new_obligation(boundary)
            if _inventory(run_dir, root_fd=boundary["run"]) != records:
                raise ValueError("隔离后投影已变化")
            _stage_records(boundary, run_dir, records, selected, stack)
            _assert_no_new_obligation(boundary)
            _purge_staged(boundary, records, selected, report)
            _verify_boundary(boundary)
            os.rmdir(boundary["batch_name"], dir_fd=boundary["agents"])
            boundary["batch_name"] = None
            if roots is None:
                _verify_boundary(boundary)
                os.rmdir(tombstone, dir_fd=boundary["agents"])
                boundary["current_name"] = None
                report["removed_dirs"] += 1
            else:
                _restore_projection(boundary)
            os.fsync(boundary["agents"])
        except BaseException as primary:
            _recover_projection(boundary, primary)
            raise


# LLM: 搬移前先初始化恢复状态并记录已建容器；两个固定FD内完整库存匹配且fsync后才准不可逆销毁。
# 函数用途: 建立私有批次并一次性隔离待删内容，不删除任何文件。
def _stage_records(boundary, run_dir, records, selected, stack):
    boundary["batch"] = None
    boundary["staged_roots"] = []
    boundary["staged_records"] = {key: records[key] for key in selected}
    name = f".purge_{boundary['run_id']}_{uuid.uuid4().hex}.files"
    os.mkdir(name, 0o700, dir_fd=boundary["agents"])
    boundary["batch_name"] = name
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=boundary["agents"])
    stack.callback(os.close, descriptor)
    boundary["batch"] = descriptor
    for root in sorted({Path(key).parts[0] for key in selected}):
        _verify_boundary(boundary)
        _rename_noreplace(boundary["run"], root, descriptor, root)
        boundary["staged_roots"].append(root)
    if _inventory(run_dir, root_fd=descriptor) != boundary["staged_records"]:
        raise ValueError("待删整批隔离后已变化")
    remaining = {key: value for key, value in records.items() if key not in boundary["staged_records"]}
    if _inventory(run_dir, root_fd=boundary["run"]) != remaining:
        raise ValueError("隔离后原投影保留项已变化")
    _verify_boundary(boundary)
    for fd in (descriptor, boundary["run"], boundary["agents"]):
        os.fsync(fd)


# LLM: 仅销毁已整批验证的隔离FD，后代父目录也核对库存物理身份；任何失败保留首因和已删计量。
# 函数用途: 在元数据投影之外删除隔离文件，不把尚未验证的原路径逐叶当作授权。
def _purge_staged(boundary, records, selected, report):
    for name in sorted(selected, key=lambda value: (len(Path(value).parts), value != "state.json", value), reverse=True):
        _assert_no_new_obligation(boundary)
        parts = Path(name).parts
        with ExitStack() as stack:
            parent_fd = boundary["batch"]
            for index, part in enumerate(parts[:-1], 1):
                parent_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
                expected = records[str(Path(*parts[:index]))]
                if _identity(os.fstat(parent_fd)) != (expected[4], expected[1]):
                    raise ValueError("隔离后代目录身份变化")
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            observed = _record_fp(info)
            if stat.S_ISDIR(info.st_mode):
                if (observed[0], observed[1], observed[4]) != (records[name][0], records[name][1], records[name][4]):
                    raise ValueError("删除时目录已变化")
                os.rmdir(parts[-1], dir_fd=parent_fd)
            else:
                if observed != records[name] or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("删除时文件已变化")
                os.unlink(parts[-1], dir_fd=parent_fd)
                report["removed_files"] += 1
                report["removed_bytes"] += info.st_size
            boundary["deleted_entries"] += 1


# LLM: 剩余项仅无覆盖移回原FD且须来自库存；未打开容器不得擅删，其尚未搬移的原投影可独立恢复。
# 函数用途: 回填未销毁的批次根并移除已空的隔离容器。
def _restore_staged(boundary):
    if boundary.get("batch_name") is None:
        return
    if boundary.get("batch") is None:
        if boundary.get("staged_roots"):
            raise ValueError("已有隔离项但缺少批次FD")
        _verify_boundary(boundary, include_batch=False)
        return
    _verify_boundary(boundary)
    with os.scandir(boundary["batch"]) as entries:
        remaining = list(entries)
    for entry in remaining:
        expected = boundary["staged_records"].get(entry.name)
        info = entry.stat(follow_symlinks=False)
        if expected is None or _identity(info) != (expected[4], expected[1]):
            raise ValueError("隔离恢复出现未知根或身份变化")
        _rename_noreplace(boundary["batch"], entry.name, boundary["run"], entry.name)
    os.rmdir(boundary["batch_name"], dir_fd=boundary["agents"])
    boundary["batch_name"] = None
    os.fsync(boundary["run"])


# LLM: 恢复失败不能掩盖首因；名称恢复不等于内容回滚，结构化结果明示部分销毁及存留隔离位置。
# 函数用途: 在异常路径尽力恢复并将可核对的恢复事实附到原异常。
def _recover_projection(boundary, primary):
    recovery_error = None
    try:
        _restore_staged(boundary)
        if boundary["current_name"] is not None and boundary["current_name"] != boundary["run_id"]:
            _restore_projection(boundary)
    except BaseException as error:
        recovery_error = type(error).__name__
    paths = [str(boundary["owner_home"] / "agents" / name)
             for name in (boundary.get("batch_name"), boundary.get("current_name"))
             if name is not None and name != boundary["run_id"]]
    partial = boundary["deleted_entries"] > 0
    primary.projection_recovery = {
        "status": "quarantined" if paths else "partial" if partial else "restored",
        "partial_deletion": partial, "quarantine_paths": paths,
        "primary_error_type": type(primary).__name__, "recovery_error_type": recovery_error,
    }


# LLM: 隔离后及每叶删除前再核真实runtime和父唤醒；不把tombstone的新目录名当运行身份。
# 函数用途: 在删叶临界区拒绝晚到的运行登记、通知和收口责任。
def _assert_no_new_obligation(boundary):
    _verify_boundary(boundary)
    status, pending = projection_runtime_facts(boundary["owner_home"], boundary["run_id"], boundary["payload"])
    if ((status and status not in AGENT_RUN_TERMINAL_STATUSES) or pending
            or _has_parent_wake(boundary["owner_home"], boundary["run_id"], boundary["payload"],
                                time_expired=boundary.get("time_expired"), wake_recheck=boundary.get("wake_recheck"))):
        raise ValueError("隔离后运行恢复或出现父唤醒")


# LLM: 只加载平台原子禁止覆盖接口；不支持时拒绝破坏性维护，绝不用stat再rename模拟。
# 函数用途: 缓存本机renameat无覆盖函数及系统标志。
@lru_cache(maxsize=1)
def _noreplace_api():
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(library, name, None)
    if function is None:
        raise OSError(errno.ENOTSUP, "平台不支持原子无覆盖重命名")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function, flag


# LLM: 源目标都是已固定父FD内单段名称，系统调用原子拒绝EEXIST，恢复和隔离共用；任何未知平台失败关闭。
# 函数用途: 把一个叶移到另一个目录FD，绝不覆盖并发出现的对象。
def _rename_noreplace(source_fd, source, target_fd, target):
    import ctypes

    for name in (source, target):
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\x00" in name:
            raise ValueError("隔离重命名必须使用单段名称")
    function, flag = _noreplace_api()
    if function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


# LLM: 内核无覆盖rename抵抗原名占位；未打开且从未承接内容的容器留证，不阻止独立核验后恢复原投影。
# 函数用途: 安全恢复投影名称，不将名称恢复说成内容事务回滚。
def _restore_projection(boundary):
    _verify_boundary(boundary, include_batch=boundary.get("batch") is not None)
    _rename_noreplace(boundary["agents"], boundary["current_name"], boundary["agents"], boundary["run_id"])
    boundary["current_name"] = boundary["run_id"]
    os.fsync(boundary["agents"])


def _cursor_projection_page(owner_home: Path, position: dict, report: dict,
                            max_dirs: int, should_stop: Callable[[], bool]):
    from ....user_space.storage_lanes import lane_directory_entry

    while report["visited_entries"] < max_dirs and not should_stop():
        name, resume, visited, complete = lane_directory_entry(
            owner_home, ("agents",), position, require_dir_fd=os.name != "nt",
        )
        report["visited_entries"] += visited
        if complete:
            report.update(page_complete=True, next_after="", directory=resume)
            return
        if name is not None:
            path = owner_home / "agents" / name
            try:
                is_directory = stat.S_ISDIR(path.lstat().st_mode)
            except FileNotFoundError:
                is_directory = False
            if is_directory:
                yield path
                report["next_after"] = name
        report["directory"] = position = resume


# LLM: 仅显式调用执行；维护分页不观察唤醒正事实，忽略独立累计位；dry-run纯读，compact/归档先耐久后删。
# 函数用途: 清理孤儿投影，七日删中间件，三十日合并归档；绝不删除工作区或唯一报告。
def maintain_owner_agent_projections(owner_home: Path, *, now: float | None = None, remove_dangling_orphans: bool = True,
                                     slim_after_days: float = 7.0, archive_after_days: float = 30.0,
                                     max_dirs: int = 5000, dry_run: bool = False, active_run_ids: set[str] | None = None,
                                     pinned_run_ids: set[str] | None = None,
                                     run_write: Callable[[Callable[[], Any]], Any] | None = None,
                                     after: str | None = None,
                                     budget_exhausted: Callable[[], bool] | None = None,
                                     wake_ref_ids: set[str] | None = None,
                                     directory_position: dict | None = None,
                                     slice_budget=None,
                                     slice_wake_lookup: Callable[[], set[str] | None] | None = None,
                                     slice_wake_usage: dict | None = None,
                                     covered_wake_lookup: Callable[[int, set[str] | None], set[str] | None] | None = None,
                                     ) -> dict[str, Any]:
    owner_home = Path(owner_home)
    current = time.time() if now is None else float(now)
    if not all(math.isfinite(value) and value >= 0 for value in (current, slim_after_days, archive_after_days)):
        raise ValueError("时间和保留期必须为非负有限数")
    if archive_after_days < slim_after_days:
        raise ValueError("归档保留期不能短于瘦身保留期")
    report = {"scanned_dirs": 0, "removed_dirs": 0, "removed_files": 0, "removed_bytes": 0,
              "slimmed_dirs": 0, "archived_dirs": 0, "would_remove_dirs": 0, "would_slim_dirs": 0,
              "would_archive_dirs": 0, "protected_dirs": 0, "errors": [], "dry_run": dry_run, "next_after": ""}
    bounded = directory_position is not None or after is not None
    report["page_complete"] = False
    if bounded:
        report.update(visited_entries=0, directory=dict(directory_position or {}), next_after=after or "")

    def time_expired():
        return slice_budget is not None and slice_budget.remaining_ms() <= 0

    def should_stop():
        return (time_expired() or (budget_exhausted is not None and budget_exhausted())
                or (slice_budget is not None and slice_wake_usage is not None and slice_budget.exhausted(
                    report.get("visited_entries", 0) + slice_wake_usage["items"], slice_wake_usage["bytes"])))

    wake_cache: list[set[str] | None] = []
    if wake_ref_ids is not None:
        wake_cache.append(wake_ref_ids)

    def wake_lookup() -> set[str] | None:
        if covered_wake_lookup is not None:
            return covered_wake_lookup(report.get("visited_entries", 0), None)
        if slice_wake_lookup is not None:
            return slice_wake_lookup()
        if not wake_cache:
            wake_cache.append(_collect_active_wake_ids(owner_home))
        return wake_cache[0]

    def wake_recheck(ids):
        return covered_wake_lookup(report.get("visited_entries", 0), ids)

    def directories():
        if bounded:
            yield from _cursor_projection_page(owner_home, report["directory"], report, max_dirs, should_stop)
        else:
            page, complete, _positive = agent_projection_page(
                owner_home, max_dirs, purpose="preview" if dry_run else "maintenance",
            )
            report["page_complete"] = complete
            for run_dir in page:
                if should_stop():
                    report["page_complete"] = False
                    return
                yield run_dir

    entries = directories()
    while True:
        try:
            run_dir = next(entries)
        except StopIteration:
            break
        except (OSError, ValueError) as exc:
            report["errors"].append({"run_id": "", "error_type": type(exc).__name__})
            break
        report["scanned_dirs"] += 1
        if not bounded:
            report["next_after"] = run_dir.name if not report["page_complete"] else ""
        info = _classify(owner_home, run_dir, now=current, active_run_ids=active_run_ids,
                         pinned_run_ids=pinned_run_ids, slim_after_days=slim_after_days, archive_after_days=archive_after_days,
                         wake_lookup=wake_lookup)
        if time_expired():
            # 当前项已检查；时间不足时保留投影，下个周期重新核验。
            report["protected_dirs"] += 1
            continue
        kind = info["classification"]
        if kind not in {"dangling_orphan", "terminal_slim_candidate", "terminal_archive_candidate"} or (kind == "dangling_orphan" and not remove_dangling_orphans):
            report["protected_dirs"] += 1
            continue
        if dry_run:
            report[{"dangling_orphan": "would_remove_dirs", "terminal_slim_candidate": "would_slim_dirs",
                    "terminal_archive_candidate": "would_archive_dirs"}[kind]] += 1
            continue
        try:
            if run_write is None:
                _maintain_projection(owner_home, run_dir, info, report, now=current, active_run_ids=active_run_ids,
                                      pinned_run_ids=pinned_run_ids, slim_after_days=slim_after_days,
                                      archive_after_days=archive_after_days,
                                      time_expired=time_expired if slice_budget is not None else None,
                                      wake_recheck=wake_recheck if covered_wake_lookup is not None else None)
            else:
                run_write(lambda run_dir=run_dir, info=info: _maintain_projection(
                    owner_home, run_dir, info, report, now=current, active_run_ids=active_run_ids,
                    pinned_run_ids=pinned_run_ids, slim_after_days=slim_after_days,
                                      archive_after_days=archive_after_days,
                                      time_expired=time_expired if slice_budget is not None else None,
                                      wake_recheck=wake_recheck if covered_wake_lookup is not None else None))
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            error = {"run_id": run_dir.name, "error_type": type(exc).__name__}
            if getattr(exc, "projection_recovery", None) is not None:
                error["recovery"] = exc.projection_recovery
            report["errors"].append(error)
    return report


# LLM: 两阶段共用本次pin快照和调度预算；投影算法不变，转录仅凭真实runtime终态及已结清唤醒退役。
# 函数用途: 将既有投影维护与终态展示流TTL接入协调器，保留未知、活跃、pin和未收口责任。
def maintain_subagents_lane(request) -> dict:
    from agent_py_agent.agent.conversation import agent_transcript
    from agent_py_agent.agent.memory_store.retention_scan import current_retention_pins

    from ....user_space.storage_lanes import (
        STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
        STORAGE_LANE_SLICE_BYTES,
        STORAGE_LANE_SLICE_ENTRIES,
        STORAGE_LANE_SLICE_MS,
        _real_directory,
        lane_cycle_generation,
        lane_directory_entry,
        owner_retention_override,
    )

    generation = lane_cycle_generation(request.cursor)
    receipt = {"cursor": dict(request.cursor), "complete": False, "items": 0, "bytes": 0,
               "cycle_generation": generation,
               "deleted_items": 0, "freed_bytes": 0, "protected": {}, "skipped": {}, "errors": []}
    if request.legal_hold:
        return {**receipt, "complete": True, "deferred_reason": "legal_hold"}
    if request.budget.exhausted():
        return receipt
    budget = request.budget.capped(
        items=getattr(request.config, "retention_scan_slice_entries", STORAGE_LANE_SLICE_ENTRIES),
        size=getattr(request.config, "retention_scan_slice_bytes", STORAGE_LANE_SLICE_BYTES),
        ms=getattr(request.config, "retention_scan_slice_ms", STORAGE_LANE_SLICE_MS),
    )
    home = Path(request.home.owner_home_dir)
    pins = None
    pin_scan = None

    def pin_snapshot():
        nonlocal pins, pin_scan
        if pins is None:
            from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget

            # 引用投影有独立的内部条目限额，与单片代理目录共享时间上界。
            pin_scan = RetentionScanBudget.default(request.config)
            pin_scan.deadline = min(pin_scan.deadline, budget.deadline)
            pins, _ = current_retention_pins(
                request.home, (), [], now=datetime.fromtimestamp(request.now, timezone.utc),
                scan_budget=pin_scan, config=request.config,
            )
        return pins

    def incomplete():
        pending = pin_scan is not None and pin_scan.exhausted()
        return {**receipt, "complete": not pending, "deferred_reason": "pins_incomplete",
                "skipped": {**receipt["skipped"], "pins_incomplete": 1}}

    def still_eligible(run_id, path):
        try:
            payload = read_projection_object(home / "agents" / run_id / "state.json", optional=True)
        except (OSError, ValueError, TypeError):
            return False
        status, pending = projection_runtime_facts(home, run_id, payload)
        return (status in AGENT_RUN_TERMINAL_STATUSES and not pending and not _pending_obligation(payload)
                and not _has_parent_wake(home, run_id, payload, time_expired=time_expired,
                    wake_recheck=lambda ids: slice_wake_lookup(candidate_ids=ids)) and pins.complete
                and run_id not in pins.ids and not pins.protects(path, []) and not time_expired())

    def retire_stream(path, observation):
        import fcntl

        # 写门可能在另一执行线程调用operation，资格回调及计量必须在真正执行的上下文中绑定。
        token = agent_transcript._RETIRE_OBSERVATION.set(observation)
        try:
            # 原发布者持owner祖先SH，原runtime写者持SQLite事务；不嵌套JSON记录锁。
            with ExitStack() as stack:
                descriptor = open_directory_beneath(home, (), require_dir_fd=True)
                stack.callback(os.close, descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for db in (runtime_db_path(home), home / "data/runtime.db"):
                    if not db.exists() and not db.is_symlink():
                        continue
                    if db.is_symlink() or db.parent.is_symlink() or not db.is_file():
                        raise ValueError("转录运行库边界无效")
                    connection = stack.enter_context(closing(sqlite3.connect(db.absolute().as_uri() + "?mode=rw", uri=True, timeout=0)))
                    connection.execute("BEGIN IMMEDIATE")
                    stack.callback(connection.rollback)
                return agent_transcript.retire_agent_transcript_file(path, now=request.now, retention_seconds=days * 86400)
        finally:
            agent_transcript._RETIRE_OBSERVATION.reset(token)

    wake_cache: list[set[str] | None] = []

    def time_expired():
        return budget.remaining_ms() <= 0

    def slice_wake_lookup(visited_entries=0, candidate_ids=None) -> set[str] | None:
        if candidate_ids is not None:
            return _covered_active_wake_ids(home, time_expired, budget=budget, usage=receipt,
                                            visited_entries=visited_entries, candidate_ids=candidate_ids)
        if not wake_cache:
            refs = _covered_active_wake_ids(home, time_expired, budget=budget, usage=receipt,
                                           visited_entries=visited_entries)
            wake_cache.append(refs)
            if refs is None:
                cursor["wake_pending"] = True
            else:
                cursor.pop("wake_pending", None)
        return wake_cache[0]

    cursor = {"phase": "projections", "cycle_generation": generation}
    cursor.update({key: value for key, value in request.cursor.items() if key in {
        "phase", "directory", "root", "workspace", "workspace_directory", "wake_pending",
    }})
    receipt["cursor"] = cursor
    if cursor["phase"] not in ("projections", "transcripts"):
        cursor.clear()
        cursor.update(phase="projections", cycle_generation=generation)
    if cursor.get("wake_pending") is True:
        # A projection can consume the last entry allowance before discovering pending wake work.
        slice_wake_lookup()
        if budget.exhausted(receipt["items"], receipt["bytes"]):
            return receipt
    phase = cursor["phase"]
    position = cursor.get("directory", {})
    if phase != "transcripts":
        if _real_directory(home, ("agents",)):
            if not pin_snapshot().complete:
                return incomplete()
            if time_expired():
                return receipt
            report = maintain_owner_agent_projections(
                home,
                now=request.now,
                max_dirs=budget.max_items,
                pinned_run_ids=pins.ids,
                run_write=request.run_write,
                directory_position=position,
                slice_budget=budget,
                slice_wake_usage=receipt,
                covered_wake_lookup=slice_wake_lookup,
            )
            receipt["items"] += report["visited_entries"]
            receipt["deleted_items"] += report["removed_dirs"] + report["slimmed_dirs"] + report["archived_dirs"]
            receipt["freed_bytes"] += report["removed_bytes"]
            receipt["protected"]["projection"] = report["protected_dirs"]
            receipt["errors"] = [{"code": "agent_projection", **error} for error in report["errors"]
                                 [:STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT]]
            cursor["directory"] = report["directory"]
            if not report["page_complete"]:
                return receipt
        wake_pending = cursor.get("wake_pending") is True
        cursor.clear()
        cursor.update(phase="transcripts", root="owner", cycle_generation=generation)
        if wake_pending:
            cursor["wake_pending"] = True
    days = owner_retention_override(request.home, "agent_transcript_days", agent_transcript.AGENT_TRANSCRIPT_RETENTION_DAYS)
    try:
        workspace_parts = (*Path(request.home.owner_workspace_dir).relative_to(home).parts, "runtime", "workspaces")
    except ValueError:
        workspace_parts = ()
    while not budget.exhausted(receipt["items"], receipt["bytes"]):
        parts = ("conversations", "agent_transcript_events")
        if cursor.get("root") == "workspaces":
            if not workspace_parts:
                break
            workspace = cursor.get("workspace")
            if workspace is None:
                try:
                    if not _real_directory(home, workspace_parts):
                        break
                    workspace, resume, visited, complete = lane_directory_entry(
                        home, workspace_parts, cursor.get("workspace_directory", {}), require_dir_fd=os.name != "nt",
                    )
                except (OSError, ValueError) as exc:
                    if len(receipt["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
                        receipt["errors"].append({"code": type(exc).__name__, "path": "/".join(workspace_parts)})
                    return receipt
                receipt["items"] += visited
                if complete:
                    break
                cursor["workspace_directory"] = resume
                if workspace is not None and _real_directory(home, (*workspace_parts, workspace, "conversations")):
                    cursor["workspace"] = workspace
                continue
            if (not isinstance(workspace, str) or not workspace or workspace in {".", ".."}
                    or any(mark in workspace for mark in ("/", "\\", "\x00"))):
                cursor.pop("workspace", None)
                cursor.pop("directory", None)
                continue
            parts = (*workspace_parts, workspace, *parts)
        previous = cursor.get("directory", {})
        try:
            if _real_directory(home, parts):
                name, resume, visited, complete = lane_directory_entry(
                    home, parts, previous, require_dir_fd=os.name != "nt",
                )
            else:
                name, resume, visited, complete = None, {}, 0, True
        except (OSError, ValueError) as exc:
            if len(receipt["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
                receipt["errors"].append({"code": type(exc).__name__, "path": "/".join(parts)})
            return receipt
        receipt["items"] += visited
        if complete:
            cursor["root"] = "workspaces"
            cursor.pop("workspace", None)
            cursor.pop("directory", None)
            continue
        cursor["directory"] = resume
        if name is None or not name.endswith(".jsonl"):
            continue
        path = home.joinpath(*parts, name)
        if time_expired():
            cursor["directory"] = previous
            return receipt
        try:
            run_id = validate_opaque_id(path.stem, kind="run_id")
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                reason = "transcript_unsafe"
            elif not info.st_mtime <= request.now - days * 86400:
                reason = "transcript_young"
            else:
                try:
                    state_doc = read_projection_object(home / "agents" / run_id / "state.json", optional=True)
                except (OSError, ValueError, TypeError):
                    state_doc = {}
                status, pending = projection_runtime_facts(home, run_id, state_doc)
                if not status:
                    reason = "transcript_unknown"
                elif status not in AGENT_RUN_TERMINAL_STATUSES or pending or _pending_obligation(state_doc) or _has_active_parent_wake(home, run_id, state_doc, wake_lookup=slice_wake_lookup):
                    reason = "transcript_active_or_pending"
                elif not pin_snapshot().complete:
                    cursor["directory"] = previous
                    return incomplete()
                elif time_expired():
                    cursor["directory"] = previous
                    return receipt
                elif run_id in pins.ids or pins.protects(path, []):
                    reason = "transcript_pinned"
                elif time_expired():
                    cursor["directory"] = previous
                    return receipt
                else:
                    observation = {"eligible": lambda run_id=run_id, path=path: still_eligible(run_id, path)}
                    freed = request.run_write(lambda path=path, observation=observation: retire_stream(path, observation))
                    if observation.get("deleted"):
                        receipt["freed_bytes"] += freed
                        receipt["deleted_items"] += 1
                        continue
                    if observation.get("lock_busy"):
                        receipt["skipped"]["lock_busy"] = receipt["skipped"].get("lock_busy", 0) + 1
                    reason = "transcript_changed_or_busy"
            receipt["protected"][reason] = receipt["protected"].get(reason, 0) + 1
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            receipt["protected"]["transcript_error"] = receipt["protected"].get("transcript_error", 0) + 1
            if len(receipt["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
                receipt["errors"].append({"code": type(exc).__name__, "path": str(path.relative_to(home))})
    else:
        return receipt
    generation = lane_cycle_generation(cursor, wrapped=True)
    return {**receipt, "cursor": {"cycle_generation": generation,
            **({"wake_pending": True} if cursor.get("wake_pending") is True else {})}, "complete": True,
            "cycle_generation": generation}


def reconcile_projection_page(agent, *, apply=False, after='', limit=100, budget=None,
                              commit_transition=None, run_ids=()):
    """显式管理员仅按准确编号归档无权威投影；缺完整目录索引不做全目录扫描。"""
    report = {'scanned_dirs': 0, 'removed_dirs': 0, 'removed_files': 0, 'removed_bytes': 0,
              'slimmed_dirs': 0, 'archived_dirs': 0, 'protected_dirs': 0,
              'errors': [], 'items': [], 'next_after': '', 'page_complete': True,
              'effect_outcome': 'not_started'}
    if not run_ids:
        report['errors'].append({'reason_code': 'PROJECTION_DISCOVERY_UNAVAILABLE',
            'next_step': '当前没有完整的只读投影目录索引；请传 run_ids 精确预览，不要全目录扫描或猜测空集合。'})
        return report
    home = Path(agent.home_paths.owner_home_dir)
    usage = {'items': 0, 'bytes': 0}
    for run_id in sorted(set(run_ids)):
        if run_id <= after:
            continue
        if report['scanned_dirs'] >= limit or (budget is not None and budget.exhausted(usage['items'], usage['bytes'])):
            report.update(page_complete=False, next_after=report['items'][-1]['run_id'] if report['items'] else after)
            break
        report['scanned_dirs'] += 1
        token = _ADMIN_RECONCILE.set((agent, budget, usage))
        try:
            info = _classify(home, home / 'agents' / run_id,
                now=time.time(), active_run_ids=None, pinned_run_ids=None, slim_after_days=7, archive_after_days=30)
            item = {key: info[key] for key in ('run_id', 'classification', 'reason', 'age_seconds')}
            item['eligible'] = info['classification'] == 'authority_missing_archive_candidate'
            item['repaired'] = False
            if not item['eligible']:
                item['next_step'] = '保留原投影；核对当前 owner 的 state/refs、父级终态、执行身份、唯一产物与未消费输入/唤醒。预算不足时请缩小 run_ids 后重新预览。'
            if item['eligible'] and apply:
                def commit():
                    _maintain_projection(home, home / 'agents' / run_id, info, report,
                        now=time.time(), active_run_ids=None, pinned_run_ids=None, slim_after_days=7, archive_after_days=30)
                    return True
                item['repaired'] = commit() if commit_transition is None else commit_transition(commit)
                report['effect_outcome'] = 'succeeded'
            elif not item['eligible']:
                report['protected_dirs'] += 1
            report['items'].append(item)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            report['errors'].append({'run_id': run_id, 'reason_code': 'PROJECTION_ARCHIVE_UNCONFIRMED', 'error_type': type(exc).__name__,
                'next_step': '保留原投影与归档；先核对原回执，不要重放原外部操作。'})
            report['effect_outcome'] = 'unknown' if apply else 'not_started'
        finally:
            _ADMIN_RECONCILE.reset(token)
    return report
