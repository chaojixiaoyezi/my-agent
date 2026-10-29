# LLM: 仅维护owner直接子代理投影，不删除locator目标或canonical工作区；活跃/欠账/未知一律保留。
# 模块用途: 显式调用的孤儿删除与七日瘦身、三十日归档服务；不自行启动后台或接入生产清理。
from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from ....common.json_io import locked_json_path, write_private_json_file_atomic
from ....common.nofollow_fs import open_readonly_file_beneath
from ....runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from ....runtime_db.schema import runtime_db_path

TERMINAL_STATUSES = frozenset({"DONE", "COMPLETED", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT"})
ORPHAN_GRACE_SECONDS = 3600.0
_SCRATCH = frozenset({"events.jsonl", "heartbeats.jsonl", "heartbeat.json", "heartbeat", "snapshots", "tmp"})
_METADATA = frozenset({"state.json", "workspace_locator.json", "refs.json", "subagent_compact.json"})
_INTERNAL_LOCKS = frozenset(f"{name}.lock" for name in _METADATA)
_SCAN_LOCK = threading.RLock()
_SCANS: OrderedDict[tuple[str, str], tuple[int, Any]] = OrderedDict()


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


# LLM: 只接受明示绝对工作区路径；缺失以stat的FileNotFound为准，权限错误不能被exists吞成孤儿。
# 函数用途: 检查locator或旧投影的工作区是否确已消失，不读取或删除目标内容。
def projection_workspace_missing(run_dir: Path, payload: dict[str, Any]) -> bool:
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
            try:
                path.stat()
            except FileNotFoundError:
                return True
            return False
    return False


# LLM: 不实例化可写Repository；只读真实WAL，冷WAL禁建sidecar；未知库/冲突状态由调用方保护。
# 函数用途: 返回同run的runtime状态及未消费闹钟；无库或无行不是活跃记录。
def projection_runtime_facts(owner_home: Path, run_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
    db = runtime_db_path(owner_home)
    if not db.exists() and not db.is_symlink():
        db = owner_home / "data/runtime.db"
    if not db.exists() and not db.is_symlink():
        return "", False
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


# LLM: scandir流式预算涵盖非目录项，保留有界LRU迭代器避免反复从头；目录换inode即重开且不跟链接。
# 函数用途: 为发现和维护各自提供至多max_dirs个目录项的公平分页，不落磁盘游标。
def agent_projection_page(owner_home: Path, max_dirs: int, *, purpose: str) -> tuple[list[Path], bool]:
    root = owner_home / "agents"
    if root.is_symlink() or not root.is_dir() or max_dirs <= 0:
        return [], True
    with _SCAN_LOCK:
        key = (str(root), purpose)
        inode = root.stat().st_ino
        cached = _SCANS.pop(key, None)
        if cached and cached[0] != inode:
            cached[1].close()
            cached = None
        entries = cached[1] if cached else os.scandir(root)
        batch = list(islice(entries, max_dirs))
        if len(batch) < max_dirs:
            entries.close()
        else:
            _SCANS[key] = (inode, entries)
        while len(_SCANS) > 32:
            _SCANS.popitem(last=False)[1][1].close()
        return [Path(entry.path) for entry in batch if entry.is_dir(follow_symlinks=False)], len(batch) < max_dirs


# LLM: 调用路径必须是owner/agents的直接实目录；读取整棵投影前拒绝逃逸和链接。
# 函数用途: 验证投影根边界；绝不将locator目标当作清理根。
def _validate_run_dir(owner_home: Path, run_dir: Path) -> None:
    if (owner_home.is_symlink() or (owner_home / "agents").is_symlink() or run_dir.is_symlink()
            or run_dir.parent != owner_home / "agents" or run_dir.name in {"", ".", ".."} or not run_dir.is_dir()):
        raise ValueError("投影目录越界或不是实目录")


# LLM: 不跟链接，拒绝特殊文件与多硬链接；物理清单同时作为删除前一致性证据及精确计量来源。
# 函数用途: 列出投影相对路径、类型、inode、尺寸和mtime，不读取正文。
def _inventory(run_dir: Path) -> dict[str, tuple[int, int, int, int]]:
    records = {}
    pending = [run_dir]
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError("投影包含链接或特殊文件")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ValueError("投影包含多链接文件")
                records[str(Path(entry.path).relative_to(run_dir))] = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
    return records


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
        for kind in ("urgent", "normal", "processing"):
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


# LLM: 目录内产物即使位于tmp/snapshots也不能按中间件删除；未知顶层文件只保留，不猜用途。
# 函数用途: 找出被交付物引用覆盖的清理根，并判定是否存在不可归档的唯一文件。
def _protected_files(run_dir: Path, payload: dict[str, Any], records: dict) -> tuple[set[str], bool]:
    roots = set()
    for value in _deliverables(payload):
        path = Path(value)
        if not path.is_absolute():
            path = run_dir / path
        try:
            relative = path.relative_to(run_dir)
        except ValueError:
            continue
        if relative.parts:
            roots.add(relative.parts[0])
    unknown = any(Path(name).parts[0] not in _SCRATCH | _METADATA | _INTERNAL_LOCKS for name in records)
    return roots, bool(roots or unknown)


# LLM: 仅投影结构与文件事实决定分类；运行库、唤醒、pin或未知强制保护；年龄不受本服务compact写入刷新。
# 函数用途: 读取一致清单并给出清理候选，返回内部证据供执行前复核。
def _classify(owner_home, run_dir, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days):
    result = {"run_id": run_dir.name, "classification": "active_or_protected", "reason": "unknown", "age_seconds": 0.0}
    try:
        _validate_run_dir(owner_home, run_dir)
        payload = read_projection_object(run_dir / "state.json")
        if payload.get("run_id", run_dir.name) != run_dir.name:
            raise ValueError("投影run_id与目录冲突")
        records = _inventory(run_dir)
        result.update(payload=payload, inventory=records)
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


# LLM: 归档锁覆盖去重、append、fsync与读回；已有run只接受合法schema，损坏档案保留原目录。
# 函数用途: 耐久保存一个终态摘要，成功前调用方不能删除投影。
def _archive_entry(path: Path, compact: dict[str, Any]) -> None:
    with locked_json_path(path):
        if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
            raise ValueError("归档不是单链接普通文件")
        found = False
        if path.exists():
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.endswith("\n"):
                        raise ValueError("归档缺少末尾换行")
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or entry.get("schema") != "subagent_compact.v1" or not entry.get("run_id"):
                        raise ValueError("归档条目损坏")
                    if entry["run_id"] == compact["run_id"]:
                        if entry != compact:
                            raise ValueError("同run归档内容冲突")
                        if found:
                            raise ValueError("归档已有重复run")
                        found = True
        with path.open("a", encoding="utf-8") as target:
            os.chmod(path, 0o600)
            if not found:
                target.write(json.dumps(compact, ensure_ascii=False, sort_keys=True) + "\n")
            target.flush()
            os.fsync(target.fileno())
        with path.open(encoding="utf-8") as source:
            if not any(json.loads(line) == compact for line in source):
                raise ValueError("归档读回失败")
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# LLM: SQLite保留写锁与既有state写者锁覆盖最终分类到删除；不初始化schema、不改行，锁忙就放弃本目录。
# 函数用途: 阻止runtime恢复/闹钟事务和正常投影写者在删除临界区提交，结束时回滚释放。
@contextmanager
def _maintenance_guard(owner_home: Path, run_dir: Path):
    with ExitStack() as stack:
        db = runtime_db_path(owner_home)
        if not db.exists() and not db.is_symlink():
            db = owner_home / "data/runtime.db"
        if db.exists() or db.is_symlink():
            if db.is_symlink() or db.parent.is_symlink() or not db.is_file():
                raise ValueError("拒绝链接runtime库")
            conn = stack.enter_context(closing(sqlite3.connect(db.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.05)))
            conn.execute("BEGIN IMMEDIATE")
            stack.callback(conn.rollback)
        # 真实投影写者自带同名锁；无锁历史夹具不凭空增加文件，保持孤儿删除精确计量。
        if (run_dir / "state.json.lock").exists():
            stack.enter_context(locked_json_path(run_dir / "state.json", blocking=False))
        yield


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


# LLM: 末次权威核验到删叶在同一维护临界区；归档失败不删原件，唯一文件保护沿分类结果保持。
# 函数用途: 执行一个候选目录的完整持锁维护动作。
def _maintain_projection(owner_home, run_dir, info, report, *, now, active_run_ids, pinned_run_ids, slim_after_days, archive_after_days):
    options = dict(now=now, active_run_ids=active_run_ids, pinned_run_ids=pinned_run_ids,
                   slim_after_days=slim_after_days, archive_after_days=archive_after_days)
    with _maintenance_guard(owner_home, run_dir):
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


# LLM: 删除只使用清单内已核inode；逐个父目录FD用NOFOLLOW打开，避免祖先链接置换后删除域外文件。
# 函数用途: 删除选中顶层中间文件或整份投影，逐文件累计实际成功删除的字节。
def _remove_records(run_dir: Path, records: dict, roots: set[str] | None, report: dict) -> None:
    if _inventory(run_dir) != records:
        raise ValueError("删除前投影已变化")
    selected = [name for name in records if roots is None or Path(name).parts[0] in roots]
    for name in sorted(selected, key=lambda value: (len(Path(value).parts), value != "state.json", value), reverse=True):
        parts = Path(name).parts
        fds = [os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            for part in parts[:-1]:
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            info = os.stat(parts[-1], dir_fd=fds[-1], follow_symlinks=False)
            observed = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns)
            if (observed[:2] != records[name][:2] if stat.S_ISDIR(info.st_mode) else observed != records[name]):
                raise ValueError("删除时投影已变化")
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(parts[-1], dir_fd=fds[-1])
            else:
                os.unlink(parts[-1], dir_fd=fds[-1])
                report["removed_files"] += 1
                report["removed_bytes"] += info.st_size
        finally:
            for fd in reversed(fds):
                os.close(fd)
    if roots is None:
        run_dir.rmdir()
        report["removed_dirs"] += 1


# LLM: 仅显式调用执行；预算扫描、dry-run纯读，保护优先；compact/归档先耐久后删，逐目录失败隔离不谎报成功。
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
        page, _complete = agent_projection_page(owner_home, max_dirs, purpose="preview" if dry_run else "maintenance")
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
            report["errors"].append({"run_id": run_dir.name, "error_type": type(exc).__name__})
    return report
