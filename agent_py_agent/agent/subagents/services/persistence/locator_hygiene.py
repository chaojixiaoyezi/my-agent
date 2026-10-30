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
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any

from ....common.json_io import write_private_json_file_atomic
from ....common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath
from ....common.opaque_id import validate_opaque_id
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
_MAINTENANCE_BOUNDARY: ContextVar[dict | None] = ContextVar("projection_maintenance_boundary", default=None)


# LLM: 只读nofollow普通文件；缺失可显式为空，损坏、链接和非对象均抛错，不能作删除许可。
# 函数用途: 为发现与维护读取同一投影结构，不写文件或创建目录。
def read_projection_object(path: Path, *, optional: bool = False) -> dict[str, Any]:
    try:
        fd = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        if optional:
            return {}
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("投影必须是JSON对象")
    return value


# LLM: locator优先于payload且字段优先序固定；发现、分类与缓存签名共用选择/校验，不读目标内容也不授删除权。
# 函数用途: 从投影中选出唯一明示绝对工作区路径，未声明时返回None。
def projection_workspace_path(run_dir: Path, payload: dict[str, Any]) -> Path | None:
    locator = read_projection_object(run_dir / "workspace_locator.json", optional=True)
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


# LLM: 单库查询绝不初始化schema，拒绝链接及冷WAL副写；任何未知库向调用方报错保留投影。
# 函数用途: 读取一个现存runtime库的运行状态和闹钟。
def _runtime_file_facts(db: Path, run_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
    if db.is_symlink() or db.parent.is_symlink() or not db.is_file():
        raise ValueError("拒绝链接runtime库")
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    if wal.is_symlink() or shm.is_symlink() or (wal.exists() and not shm.is_file()):
        raise ValueError("拒绝链接runtime旁文件")
    with db.open("rb") as source:
        immutable = not wal.exists() and source.read(20)[18:20] == b"\x02\x02"
    uri = db.absolute().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as conn:
        conn.execute("PRAGMA query_only=ON")
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
def _has_parent_wake(owner_home: Path, run_id: str, payload: dict[str, Any]) -> bool:
    ids = {run_id, str(payload.get("root_run_id") or payload.get("root_id") or ""), str(payload.get("task_id") or "")} - {""}
    roots = [owner_home / "conversations", owner_home / "data/conversations"]
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
                    if checked > 5000:
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


# LLM: 分类同时固定物理父链和FD清单供执行复核；运行库、唤醒、pin或未知强制保护，compact不刷新年龄。
# 函数用途: 读取一致清单并给出清理候选，返回内部证据供执行前复核。
def _classify(owner_home, run_dir, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days):
    result = {"run_id": run_dir.name, "classification": "active_or_protected", "reason": "unknown", "age_seconds": 0.0}
    try:
        _validate_run_dir(owner_home, run_dir)
        payload = read_projection_object(run_dir / "state.json")
        if payload.get("run_id", run_dir.name) != run_dir.name:
            raise ValueError("投影run_id与目录冲突")
        with _pinned_projection(owner_home, run_dir) as boundary:
            records = _inventory(run_dir, root_fd=boundary["run"])
            result.update(payload=payload, inventory=records, directory_ids=boundary["ids"])
        if run_dir.name in (active_run_ids or set()) | (pinned_run_ids or set()) or _pending_obligation(payload):
            result["reason"] = "explicit_protection"
            return result
        runtime_status, pending = projection_runtime_facts(owner_home, run_dir.name, payload)
        if (runtime_status and runtime_status not in AGENT_RUN_TERMINAL_STATUSES) or pending or _has_parent_wake(owner_home, run_dir.name, payload):
            result["reason"] = "runtime_or_wake_active"
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
        if projection_workspace_missing(run_dir, payload) and age >= ORPHAN_GRACE_SECONDS and not unique:
            result.update(classification="dangling_orphan", reason="workspace_missing")
            return result
        if str(payload.get("status", "")).upper() not in TERMINAL_STATUSES:
            result["reason"] = "nonterminal_or_unknown"
            return result
        scratch = {Path(name).parts[0] for name in records} & _SCRATCH - protected_roots
        result["scratch_roots"] = scratch
        if age >= archive_after_days * 86400 and not unique:
            result.update(classification="terminal_archive_candidate", reason="archive_age")
        elif age >= slim_after_days * 86400 and scratch:
            result.update(classification="terminal_slim_candidate", reason="slim_age")
        elif "subagent_compact.json" in records and not scratch:
            result.update(classification="terminal_compact_stub_only", reason="already_slim")
        else:
            result["reason"] = "young_or_unique"
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError) as exc:
        result["reason"] = f"unreadable:{type(exc).__name__}"
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
            "created_at": payload.get("created_at"), "updated_at": payload.get("updated_at"),
            "result_summary": payload.get("result_summary", payload.get("last_progress_summary", "")),
            "deliverable_paths": _deliverables(payload), "error_summary": payload.get("error_summary", payload.get("error", "")),
            "usage_summary": payload.get("usage_summary", payload.get("usage", {}))}


# LLM: 归档在固定agents FD内NOFOLLOW打开，持同一普通文件FD锁完成去重、追加、同步及读回；路径变更拒绝删除原件。
# 函数用途: 耐久保存一个终态摘要，成功前调用方不能删除投影。
def _archive_entry(path: Path, compact: dict[str, Any]) -> None:
    import fcntl

    boundary = _MAINTENANCE_BOUNDARY.get()
    if boundary is None or path.parent != boundary["owner_home"] / "agents":
        raise ValueError("归档缺少受信维护边界")
    if (not isinstance(compact, dict) or compact.get("schema") != "subagent_compact.v1"
            or compact.get("run_id") != boundary["run_id"]):
        raise ValueError("归档新条目schema或run_id不符")
    validate_opaque_id(compact["run_id"], kind="run_id")
    _verify_boundary(boundary)
    with ExitStack() as stack:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600, dir_fd=boundary["agents"])
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _verify_archive_leaf(boundary, path.name, fd)
        target = stack.enter_context(os.fdopen(os.dup(fd), "a+", encoding="utf-8"))
        target.seek(0)
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
        os.fsync(boundary["agents"])


# LLM: FD仍可读不代表路径仍指向该归档；单链接、普通类型和当前路径身份同时成立才准继续。
# 函数用途: 核对归档打开前后未被换名、链接或脱链。
def _verify_archive_leaf(boundary, name, fd):
    _verify_boundary(boundary)
    info = os.fstat(fd)
    current = os.stat(name, dir_fd=boundary["agents"], follow_symlinks=False)
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
def _verify_before_delete(owner_home, run_dir, info, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days):
    fresh = _classify(owner_home, run_dir, now=now, active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                      slim_after_days=slim_after_days, archive_after_days=archive_after_days)
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
def _maintain_projection(owner_home, run_dir, info, report, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days):
    options = dict(now=now, active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                   slim_after_days=slim_after_days, archive_after_days=archive_after_days)
    with _maintenance_guard(owner_home, run_dir, expected=info["directory_ids"]) as boundary:
        boundary["payload"] = info["payload"]
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
            if kind == "terminal_archive_candidate":
                archive = owner_home / "agents" / datetime.fromtimestamp(now, timezone.utc).strftime("archived_subagents_%Y_%m.jsonl")
                _archive_entry(archive, _compact(info["compact_source"], run_dir.name))
                records = _verify_before_delete(owner_home, run_dir, info, **options)
            _remove_records(run_dir, records, None, report)
            if kind == "terminal_archive_candidate":
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
            or _has_parent_wake(boundary["owner_home"], boundary["run_id"], boundary["payload"])):
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


# LLM: 仅显式调用执行；维护分页不观察唤醒正事实，忽略独立累计位；dry-run纯读，compact/归档先耐久后删。
# 函数用途: 清理孤儿投影，七日删中间件，三十日合并归档；绝不删除工作区或唯一报告。
def maintain_owner_agent_projections(owner_home: Path, *, now: float | None = None, remove_dangling_orphans: bool = True,
                                     slim_after_days: float = 7.0, archive_after_days: float = 30.0,
                                     max_dirs: int = 5000, dry_run: bool = False, active_run_ids: set[str] | None = None,
                                     pinned_run_ids: set[str] | None = None) -> dict[str, Any]:
    owner_home = Path(owner_home)
    current = time.time() if now is None else float(now)
    if not all(math.isfinite(value) and value >= 0 for value in (current, slim_after_days, archive_after_days)):
        raise ValueError("时间和保留期必须为非负有限数")
    if archive_after_days < slim_after_days:
        raise ValueError("归档保留期不能短于瘦身保留期")
    report = {"scanned_dirs": 0, "removed_dirs": 0, "removed_files": 0, "removed_bytes": 0,
              "slimmed_dirs": 0, "archived_dirs": 0, "would_remove_dirs": 0, "would_slim_dirs": 0,
              "would_archive_dirs": 0, "protected_dirs": 0, "errors": [], "dry_run": dry_run}
    try:
        page, _complete, _positive = agent_projection_page(owner_home, max_dirs, purpose="preview" if dry_run else "maintenance")
    except OSError as exc:
        report["errors"].append({"run_id": "", "error_type": type(exc).__name__})
        return report
    for run_dir in page:
        report["scanned_dirs"] += 1
        info = _classify(owner_home, run_dir, now=current, active_run_ids=active_run_ids,
                         pinned_run_ids=pinned_run_ids, slim_after_days=slim_after_days, archive_after_days=archive_after_days)
        kind = info["classification"]
        if kind not in {"dangling_orphan", "terminal_slim_candidate", "terminal_archive_candidate"} or (kind == "dangling_orphan" and not remove_dangling_orphans):
            report["protected_dirs"] += 1
            continue
        if dry_run:
            report[{"dangling_orphan": "would_remove_dirs", "terminal_slim_candidate": "would_slim_dirs",
                    "terminal_archive_candidate": "would_archive_dirs"}[kind]] += 1
            continue
        try:
            _maintain_projection(owner_home, run_dir, info, report, now=current, active_run_ids=active_run_ids,
                                  pinned_run_ids=pinned_run_ids, slim_after_days=slim_after_days, archive_after_days=archive_after_days)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            error = {"run_id": run_dir.name, "error_type": type(exc).__name__}
            if getattr(exc, "projection_recovery", None) is not None:
                error["recovery"] = exc.projection_recovery
            report["errors"].append(error)
    return report
