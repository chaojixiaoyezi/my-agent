
from __future__ import annotations

"""Gateway daemon metadata helpers shared by PID, lock, and status modules."""

import hashlib
import json
import os
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


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _build_pid_record() -> dict:
    return {
        "pid": os.getpid(),
        "kind": "my-agent-gateway",
        "argv": list(sys.argv),
        "start_time": _get_process_start_time(os.getpid()),
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
