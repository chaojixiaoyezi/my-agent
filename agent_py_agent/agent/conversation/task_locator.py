"""可重建的任务位置投影；它只定位，不能授予 owner、路径或任务状态权限。"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..user_space.home_indexes import (
    TaskRefLookupScope,
    is_sqlite_busy,
    lookup_task_ref,
    set_task_ref_meta,
    task_ref_lookup_scope,
    task_ref_meta,
    task_ref_projection,
)

TASK_LOCATOR_SCAN_MAX_FILES = 128
_ENUM_CACHE_CAPACITY = 8
_ENUM_LOCK = threading.Lock()


@dataclass
class _Enumeration:
    iterator: object
    position: int


# 只缓存至多八个尚未读完的目录枚举器；重启后从耐久位置重放目录名，不重读旧 state。
_ENUMERATIONS: OrderedDict[tuple[str, str], _Enumeration] = OrderedDict()


def _directory_signature(root: Path, directory: Path) -> str:
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        info = current.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe legacy task directory")
    info = directory.stat(follow_symlinks=False)
    return ":".join(str(v) for v in (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))


def _directory_page(scope: TaskRefLookupScope, directory: Path, signature: str, position: int, limit: int) -> tuple[list, bool, int]:
    key = (f"{scope.owner_root}:{scope.owner_id}:{directory}", signature)
    replayed = 0
    with _ENUM_LOCK:
        entry = _ENUMERATIONS.pop(key, None)
        if entry is not None and entry.position != position:
            entry.iterator.close()
            entry = None
        if entry is None:
            entry = _Enumeration(os.scandir(directory), 0)
            while entry.position < position:
                try:
                    next(entry.iterator)
                except StopIteration:
                    entry.iterator.close()
                    return [], True, replayed
                entry.position += 1
                replayed += 1
        rows = []
        done = False
        try:
            for _ in range(limit):
                try:
                    rows.append(next(entry.iterator))
                    entry.position += 1
                except StopIteration:
                    done = True
                    break
        except BaseException:
            entry.iterator.close()
            raise
        if done:
            entry.iterator.close()
        else:
            _ENUMERATIONS[key] = entry
            while len(_ENUMERATIONS) > _ENUM_CACHE_CAPACITY:
                _, evicted = _ENUMERATIONS.popitem(last=False)
                evicted.iterator.close()
    return rows, done, replayed


def _read_candidate(scope: TaskRefLookupScope, candidate: str | Path, task_id: str = "") -> tuple[Path | None, dict, str]:
    """位置先过 owner 根与 no-follow 边界，再核真实 state 的 task/owner。"""
    try:
        path = Path(candidate)
        if not path.is_absolute():
            path = scope.owner_root / path
        relative = path.relative_to(scope.owner_root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            return None, {}, "outside_owner"
        raw = read_bytes_beneath(scope.owner_root, (*relative.parts, "work", "state.json"), max_bytes=1048576)
        if raw is None:
            return None, {}, "missing_state"
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("task_id"), str) or not payload["task_id"]:
            return None, {}, "invalid_state"
        if task_id and payload["task_id"] != task_id:
            return None, {}, "task_mismatch"
        # 旧 producer 没有 owner_id 字段时，可信 owner 根本身提供归属边界；
        # 已有显式字段必须精确相同，绝不能信任索引里声称的 owner。
        if payload.get("owner_id") not in (None, "", scope.owner_id):
            return None, {}, "owner_mismatch"
        return path, payload, ""
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return None, {}, "unreadable_state"


def validate_task_location(owner_home: object, candidate: str | Path, task_id: str) -> Path | None:
    """供已有 link.task_path 快路复用；不建索引、不扫 tasks、不改变业务状态。"""
    try:
        scope = task_ref_lookup_scope(owner_home)
        return _read_candidate(scope, candidate, task_id)[0]
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def _reset_legacy(scope: TaskRefLookupScope, conn: sqlite3.Connection, signature: str) -> None:
    conn.execute("DELETE FROM task_refs WHERE origin='legacy'")
    conn.execute("DELETE FROM legacy_dirs")
    conn.execute("INSERT INTO legacy_dirs(path,depth) VALUES (?,0)", (str(scope.owner_root / "tasks"),))
    for key, value in (("legacy.signature", signature), ("legacy.generation", uuid.uuid4().hex),
                       ("legacy.total", 0), ("legacy.errors", 0), ("legacy.done", 0), ("legacy.audit", "")):
        set_task_ref_meta(conn, key, value)


def _forget_legacy_directory(conn: sqlite3.Connection, directory: Path) -> None:
    prefix = str(directory) + os.sep
    conn.execute("DELETE FROM task_refs WHERE origin='legacy' AND substr(path,1,?)=?", (len(prefix), prefix))


def _refresh_completed_directories(scope: TaskRefLookupScope, conn: sqlite3.Connection) -> bool:
    """负查询检查已完成目录的 revision，仍按 128 个目录一片，不全库 stat。"""
    after = task_ref_meta(conn, "legacy.audit")
    rows = conn.execute("SELECT path,signature FROM legacy_dirs WHERE depth=1 AND path>? ORDER BY path LIMIT ?",
                        (after, TASK_LOCATOR_SCAN_MAX_FILES)).fetchall()
    changed = False
    for name, previous in rows:
        directory = Path(name)
        try:
            signature = _directory_signature(scope.owner_root, directory)
        except FileNotFoundError:
            _forget_legacy_directory(conn, directory)
            conn.execute("DELETE FROM legacy_dirs WHERE path=?", (name,))
            continue
        if signature != previous:
            _forget_legacy_directory(conn, directory)
            conn.execute("UPDATE legacy_dirs SET position=0,signature=?,done=0 WHERE path=?", (signature, name))
            changed = True
    last = rows[-1][0] if rows else after
    more = conn.execute("SELECT 1 FROM legacy_dirs WHERE depth=1 AND path>? LIMIT 1", (last,)).fetchone() is not None
    set_task_ref_meta(conn, "legacy.audit", last if more else "")
    return not more and not changed


def _legacy_slice(scope: TaskRefLookupScope, conn: sqlite3.Connection) -> dict:
    root = scope.owner_root / "tasks"
    try:
        signature = _directory_signature(scope.owner_root, root)
    except FileNotFoundError:
        return {"legacy_files_scanned": 0, "legacy_total_scanned": 0, "legacy_complete": True,
                "legacy_errors": 0, "directory_entries_replayed": 0}
    if task_ref_meta(conn, "legacy.signature") != signature:
        _reset_legacy(scope, conn, signature)
    was_done = task_ref_meta(conn, "legacy.done") == "1"
    if was_done:
        if int(task_ref_meta(conn, "legacy.errors", "0")):
            _reset_legacy(scope, conn, signature)
        else:
            complete = _refresh_completed_directories(scope, conn)
            pending = conn.execute("SELECT 1 FROM legacy_dirs WHERE done=0 LIMIT 1").fetchone() is not None
            # done 表示目录枚举追平，不代表分片 revision 复核已结束；不能因
            # 下一页没有待枚举目录就提前跳过尚未复核的日期目录。
            set_task_ref_meta(conn, "legacy.done", int(not pending))
            if not pending:
                return {"legacy_files_scanned": 0,
                        "legacy_total_scanned": int(task_ref_meta(conn, "legacy.total", "0")),
                        "legacy_complete": complete, "legacy_errors": 0, "directory_entries_replayed": 0}
    remaining = TASK_LOCATOR_SCAN_MAX_FILES
    files = replayed = 0
    errors = int(task_ref_meta(conn, "legacy.errors", "0"))
    while remaining:
        row = conn.execute("SELECT path,depth,position,signature FROM legacy_dirs WHERE done=0 ORDER BY depth DESC,path LIMIT 1").fetchone()
        if row is None:
            break
        name, depth, position, previous = row
        directory = Path(name)
        try:
            current = _directory_signature(scope.owner_root, directory)
        except FileNotFoundError:
            _forget_legacy_directory(conn, directory)
            conn.execute("UPDATE legacy_dirs SET done=1 WHERE path=?", (name,))
            remaining -= 1
            continue
        if previous and current != previous:
            position = 0
            _forget_legacy_directory(conn, directory)
        entries, done, skipped = _directory_page(scope, directory, current, position, remaining)
        replayed += skipped
        remaining -= max(1, len(entries))
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if depth == 0:
                conn.execute("INSERT OR IGNORE INTO legacy_dirs(path,depth) VALUES (?,1)", (entry.path,))
                continue
            files += 1
            path, state, error = _read_candidate(scope, entry.path)
            if path is not None:
                conn.execute("INSERT OR IGNORE INTO task_refs VALUES (?,?,?,'legacy')",
                             (scope.owner_id, state["task_id"], str(path)))
            elif error not in {"missing_state", "owner_mismatch", "task_mismatch", "outside_owner"}:
                errors += 1
        if _directory_signature(scope.owner_root, directory) != current:
            raise OSError("legacy task directory changed during slice")
        conn.execute("UPDATE legacy_dirs SET position=?,signature=?,done=? WHERE path=?",
                     (position + len(entries), current, int(done), name))
    if _directory_signature(scope.owner_root, root) != signature:
        raise OSError("legacy task root changed during slice")
    complete = conn.execute("SELECT 1 FROM legacy_dirs WHERE done=0 LIMIT 1").fetchone() is None
    total = int(task_ref_meta(conn, "legacy.total", "0")) + files
    set_task_ref_meta(conn, "legacy.total", total)
    set_task_ref_meta(conn, "legacy.errors", errors)
    set_task_ref_meta(conn, "legacy.done", int(complete))
    return {"legacy_files_scanned": files, "legacy_total_scanned": total,
            "legacy_complete": complete and not errors and not task_ref_meta(conn, "legacy.audit"),
            "legacy_errors": errors, "directory_entries_replayed": replayed}


def _location_result(status: str, coverage: dict, *, path: Path | None = None, cursor: str | None = None,
                     reason: str = "") -> dict:
    return {"status": status, "path": str(path) if path is not None else "", "cursor": cursor,
            "coverage": coverage, "reason": reason}


def resolve_task_location(owner_home: object, task_id: str, *, cursor: str | None = None) -> dict:
    """沿唯一 task-ref 发布链定位，旧目录每次最多回填 128 个 state。

    cursor 只是调用者保存的续接提示；真正检查点在 owner 绑定的可重建投影里。
    不从传入 cursor 恢复路径/身份，也不从投影里的 status 推断任务完成。
    """
    coverage = {"index_complete": False, "index_records_read": 0, "index_errors": 0,
                "legacy_files_scanned": 0, "legacy_total_scanned": 0, "legacy_complete": False,
                "legacy_errors": 0, "directory_entries_replayed": 0,
                "legacy_file_budget": TASK_LOCATOR_SCAN_MAX_FILES}
    if not isinstance(task_id, str) or not task_id:
        return _location_result("partial", coverage, reason="invalid_task_id")
    try:
        scope = task_ref_lookup_scope(owner_home)
        indexed = lookup_task_ref(owner_home, task_id, max_records=TASK_LOCATOR_SCAN_MAX_FILES)
        coverage.update(indexed["coverage"])
        if indexed["status"] == "partial":
            return _location_result("partial", coverage, cursor=f"index:{coverage['index_offset']}",
                                    reason="index_backfill_deferred")
        valid = []
        uncertain = False
        for candidate in indexed["paths"]:
            path, _, error = _read_candidate(scope, candidate, task_id)
            if path is not None and path not in valid:
                valid.append(path)
            elif error in {"invalid_state", "unreadable_state"}:
                uncertain = True
        if len(valid) == 1:
            return _location_result("found", coverage, path=valid[0])
        if len(valid) > 1:
            return _location_result("partial", coverage, reason="ambiguous_task_locations")
        with task_ref_projection(owner_home) as (scope, conn):
            # 文件暂时消失/读坏不能抹掉 publisher 的位置提示，否则源没有追加时
            # 永远无法再发现已恢复的自定义路径。每次候选仍须重新核 canonical state。
            coverage.update(_legacy_slice(scope, conn))
            rows = conn.execute("SELECT path FROM task_refs WHERE owner_id=? AND task_id=? AND origin='legacy' LIMIT 17",
                                (scope.owner_id, task_id)).fetchall()
            valid = []
            for (candidate,) in rows:
                path, _, _ = _read_candidate(scope, candidate, task_id)
                if path is not None and path not in valid:
                    valid.append(path)
            checkpoint = f"legacy:{task_ref_meta(conn, 'legacy.generation')}:{coverage['legacy_total_scanned']}"
        if len(valid) == 1:
            return _location_result("found", coverage, path=valid[0])
        if len(valid) > 1:
            return _location_result("partial", coverage, cursor=checkpoint, reason="ambiguous_task_locations")
        if coverage["legacy_complete"] and not coverage.get("index_errors_total", 0) and not uncertain:
            return _location_result("missing", coverage)
        if uncertain:
            return _location_result("partial", coverage, cursor=checkpoint, reason="canonical_state_unreadable")
        if coverage.get("index_errors_total", 0):
            return _location_result("partial", coverage, cursor=checkpoint, reason="task_ref_source_incomplete")
        return _location_result("partial", coverage, cursor=checkpoint, reason="legacy_backfill_deferred")
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        # 锁忙、损坏和挂载消失都是未完成观察，不是 not_found，更不能触发收口。
        return _location_result("partial", coverage, cursor=cursor,
                                reason="sqlite_busy" if is_sqlite_busy(error) else type(error).__name__)
