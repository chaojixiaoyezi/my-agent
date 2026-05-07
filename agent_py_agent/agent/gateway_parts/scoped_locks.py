# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Machine-local scoped locks for gateway identities."""

import json
import os
from pathlib import Path
from typing import Any

from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _read_json_file,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)
from .process_control import is_pid_alive


# LLM: _get_lock_dir 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询锁dir需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _get_lock_dir() -> Path:
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "my-agent" / "locks"


# LLM: _get_scope_lock_path 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询scope锁路径需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


# LLM: _build_scope_lock_record 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建scope锁记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _build_scope_lock_record(scope: str, identity: str, metadata: dict[str, Any] | None) -> dict:
    return {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }


# LLM: _lock_pid 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理锁pid相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _lock_pid(record: dict | None) -> int | None:
    try:
        return int((record or {})["pid"])
    except (KeyError, TypeError, ValueError):
        return None


# LLM: _owns_lock 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理owns锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _owns_lock(existing: dict, record: dict) -> bool:
    pid = _lock_pid(existing)
    return pid == os.getpid() and existing.get("start_time") == record.get("start_time")


# LLM: _lock_process_stale 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理锁processstale相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _lock_process_stale(existing: dict) -> bool:
    pid = _lock_pid(existing)
    if pid is None:
        return True
    # LLM: 作用域锁复用网关进程存活探测，保证 Windows 与 macOS 路径判断一致。
    if not is_pid_alive(pid):
        return True
    current_start = _get_process_start_time(pid)
    recorded_start = existing.get("start_time")
    return recorded_start is not None and current_start is not None and current_start != recorded_start


# LLM: _remove_lock_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理remove锁文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _remove_lock_file(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# LLM: _create_lock_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建锁文件所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _create_lock_file(lock_path: Path, record: dict) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        _remove_lock_file(lock_path)
        raise
    return True


# LLM: acquire_scoped_lock 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理acquirescoped锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def acquire_scoped_lock(
    scope: str, identity: str, metadata: dict[str, Any] | None = None
) -> tuple[bool, dict | None]:
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_scope_lock_record(scope, identity, metadata)
    existing = _read_json_file(lock_path)
    if existing and _owns_lock(existing, record):
        _write_json_file(lock_path, record)
        return True, existing
    if existing and not _lock_process_stale(existing):
        return False, existing
    if existing:
        _remove_lock_file(lock_path)
    if not _create_lock_file(lock_path, record):
        return False, _read_json_file(lock_path)
    return True, None


# LLM: release_scoped_lock 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理releasescoped锁相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def release_scoped_lock(scope: str, identity: str) -> None:
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing or existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    _remove_lock_file(lock_path)


# LLM: release_all_scoped_locks 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理releaseallscopedlocks相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def release_all_scoped_locks() -> int:
    lock_dir = _get_lock_dir()
    if not lock_dir.exists():
        return 0
    removed = 0
    for lock_file in lock_dir.glob("*.lock"):
        if _release_lock_if_stale(lock_file):
            removed += 1
    return removed


# LLM: _release_lock_if_stale 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理release锁ifstale相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _release_lock_if_stale(lock_file: Path) -> bool:
    record = _read_json_file(lock_file)
    if not record:
        return False
    try:
        pid = int(record["pid"])
    except (ProcessLookupError, PermissionError, ValueError):
        lock_file.unlink()
        return True
    # LLM: stale lock cleanup must use the same cross-platform PID probe as gateway status.
    if not is_pid_alive(pid):
        lock_file.unlink()
        return True
    if _get_process_start_time(pid) != record.get("start_time"):
        lock_file.unlink()
        return True
    return False
