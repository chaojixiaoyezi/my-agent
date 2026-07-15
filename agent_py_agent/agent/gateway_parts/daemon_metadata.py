
from __future__ import annotations

"""Gateway daemon metadata helpers shared by PID, lock, and status modules."""

import hashlib
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_process_start_time(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks).
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


# LLM: 后台租约只能在“同一进程域且旧 PID 身份已死”时提前接管；machine-id 还要叠加
# Linux PID namespace，避免多个 Kubernetes Pod 共享 machine-id 后互相把不可见 PID 误判为已死。
# 函数用途: 生成当前主机/容器进程域标识，供 PID+start_time 租约身份判定复用。
def process_host_id() -> str:
    identity_parts: list[str] = []
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            identity_parts.append(value)
            break
    if not identity_parts:
        identity_parts.append(socket.gethostname().strip() or "unknown-host")
    try:
        identity_parts.append(os.readlink("/proc/self/ns/pid"))
    except OSError:
        pass
    raw = "\n".join(identity_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# LLM: 进程身份由 host_id+pid+start_time 组成，避免只凭 PID 在复用后误认旧执行者仍存活。
# 函数用途: 为当前或指定进程构造可持久化、可核验的运行身份。
def build_process_identity(pid: int | None = None) -> dict[str, object]:
    process_id = os.getpid() if pid is None else int(pid)
    return {
        "host_id": process_host_id(),
        "pid": process_id,
        "start_time": _get_process_start_time(process_id),
    }


# LLM: 返回 None 表示跨进程域或字段不足，调用方必须按 TTL fail-safe；只有同域且能证明
# PID 已死或 start_time 不符时才返回 False，绝不能把“看不见”当成“已死”。
# 函数用途: 核验一个持久化进程身份现在是否仍代表同一个活进程。
def process_identity_is_live(identity: object) -> bool | None:
    if not isinstance(identity, dict):
        return None
    host_id = str(identity.get("host_id") or "").strip()
    if not host_id or host_id != process_host_id():
        return None
    try:
        pid = int(identity.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    from .process_control import is_pid_alive

    if not is_pid_alive(pid):
        return False
    recorded_start = identity.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and recorded_start != current_start:
        return False
    return True


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _build_pid_record() -> dict:
    identity = build_process_identity()
    return {
        **identity,
        "kind": "my-agent-gateway",
        "argv": list(sys.argv),
        "updated_at": _utc_now_iso(),
    }


def _read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_json_file(path: Path, payload: dict) -> None:
    # H8:原 path.write_text 非原子,写一半崩溃/磁盘满会留半截 JSON。daemon_metadata
    # 写的是 PID 记录 / status / scoped-lock 心跳——半截锁文件会被
    # scoped_locks._read_lock_record_report 判 lock_load_error,acquire_scoped_lock
    # 遇 load_error 直接拒绝接管,gateway 身份永久卡死。改 temp+os.replace 原子落盘:
    # 读取方要么看到旧的完整记录、要么看到新的完整记录,绝无半截。同名 sidecar 上加
    # fcntl.flock(LOCK_EX) 串行化并发写,与 io/jsonl.py、common/json_io.py 同一手法。
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _flocked_sidecar(path):
        try:
            tmp.write_text(content, encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            _unlink_quiet(tmp)


@contextmanager
def _flocked_sidecar(path: Path):
    # 串行化 sidecar 后缀【刻意】用 .wlock 而非 .lock:scoped_locks 锁目录里
    # release_all_scoped_locks 用 glob("*.lock") 扫描,若写锁 sidecar 也叫 .lock
    # 会被误当成一把"空锁记录"扫到(虽因 load_error 判定不会误删,但污染目录)。
    lock_path = path.with_name(path.name + ".wlock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        _flock(lock_handle, exclusive=True)
        try:
            yield
        finally:
            _flock(lock_handle, exclusive=False)


def _flock(handle, *, exclusive: bool) -> None:
    if fcntl is None:
        if exclusive:
            from ..common.file_lock_support import warn_file_lock_unavailable_once
            warn_file_lock_unavailable_once()
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)


def _unlink_quiet(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


def _replace_with_retry(tmp: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:  # Windows: rename over open target may transiently fail.
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    if last_error is not None:
        raise last_error
