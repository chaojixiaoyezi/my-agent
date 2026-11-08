"""数据库代次门：连接持 SH 至物理关闭；恢复意图阻止新准入，EX 才证明排空。"""
from __future__ import annotations

import ctypes
import os
import sqlite3
import threading
from contextlib import AbstractContextManager as ContextManager
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..common import json_io

_EXCLUSIVE = ContextVar("runtime_database_exclusive", default=None)
_CONNECTIONS = threading.local()
_HELD_CONNECTIONS = {}
_HELD_LOCK = threading.Lock()


def _after_fork():
    global _HELD_LOCK
    _HELD_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class RuntimeDatabaseDeferred(sqlite3.OperationalError):
    """可由原请求/维护入口重试的资源事实，不代表损坏或已静止。"""
    sqlite_errorcode = sqlite3.SQLITE_BUSY
    sqlite_errorname = "SQLITE_BUSY"
    status = "deferred"
    code = "RUNTIME_DB_RECOVERY_DEFERRED"

    def __init__(self, reason):
        self.reason = reason
        super().__init__(self.code + ": " + reason)


def _path(path):
    return Path(os.path.abspath(path))


def _intent_path(path):
    return _path(path).with_name(Path(path).name + ".recovery-intent.json")


def runtime_db_recovery_pending(path):
    return os.path.lexists(_intent_path(path))


def runtime_db_exclusive_owned(path):
    active = _EXCLUSIVE.get()
    return bool(active is not None and active[3] and tuple(active[:2]) ==
                (_path(path), (os.getpid(), threading.current_thread())))


def request_runtime_db_recovery(path):
    """意图跨进程持久保留；失败的 EX 不清除它，也不借 TTL 推断执行体死亡。"""
    intent = _intent_path(path)
    try:
        with json_io.locked_json_path(intent, blocking=False):
            if not os.path.lexists(intent):
                json_io.write_private_text_file_atomic_unlocked(intent, '{"schema":"runtime_db_recovery_intent.v1"}\n')
    except BlockingIOError as error:
        raise RuntimeDatabaseDeferred("recovery_intent_busy") from error


class _WindowsOverlapped(ctypes.Structure):
    """保留 Win32 指针宽度与双 DWORD 偏移；每次锁请求单独归零。"""

    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


def _lock_windows_generation(descriptor, *, exclusive, blocking):
    """原字节区间提供真正的 SH/EX，避免独立连接句柄互相排斥。"""
    if os.name != "nt" or json_io.msvcrt is None or not hasattr(ctypes, "windll"):
        json_io._flock_descriptor(descriptor, blocking=blocking)
        return
    lock_file = ctypes.windll.kernel32.LockFileEx
    lock_file.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_WindowsOverlapped))
    lock_file.restype = ctypes.c_int
    overlapped = _WindowsOverlapped()
    flags = (0x2 if exclusive else 0x0) | (0x0 if blocking else 0x1)
    if not lock_file(json_io.msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.GetLastError()
        if error in {33, 5, 32}:
            raise BlockingIOError(error, "数据库代次锁繁忙")
        raise ctypes.WinError(error)


@contextmanager
def runtime_db_generation_guard(path: Path, *, exclusive: bool = False, blocking: bool = False) -> ContextManager[None]:
    """锁侧车永不换 inode；各平台以 SH 保连接、EX 证明恢复前已排空。"""
    path = _path(path)
    owner = (os.getpid(), threading.current_thread())
    active = _EXCLUSIVE.get()
    if not exclusive and runtime_db_exclusive_owned(path):
        # 同一 EX 内的 salvage 子连接仍持有同一 open-file-description，外层退出也不能提前解锁。
        descriptor = os.dup(active[2])
        try:
            yield
        finally:
            os.close(descriptor)
        return
    if exclusive:
        request_runtime_db_recovery(path)
        retire_idle_runtime_connections(path)
    elif runtime_db_recovery_pending(path):
        raise RuntimeDatabaseDeferred("recovery_pending")
    lock = path.with_name(path.name + ".generation.lock")
    if json_io.fcntl is None and json_io.msvcrt is None:
        raise RuntimeDatabaseDeferred("generation_lock_unavailable")
    # 与原工厂共享弱注册表，但保持连接键的词法路径合同，不为每次借用新增 realpath。
    with json_io._JSON_FILE_LOCKS_GUARD:
        handle = json_io._JSON_FILE_LOCKS.get(str(lock))
        if handle is None:
            handle = json_io._PathLock()
            json_io._JSON_FILE_LOCKS[str(lock)] = handle
    descriptor = None
    state, token, completed = None, None, False
    try:
        # 进程内互斥只串行本进程“开锁FD+flock”这一有界临界区；同进程线程同时建连不是代次事实，
        # 须等待而不是报 generation_busy。是否阻塞仍只由下面的 flock 按 blocking 决定。
        handle.lock.acquire()
        try:
            descriptor = json_io._open_private_lock_descriptor(lock)
            try:
                if json_io.fcntl is not None:
                    mode = json_io.fcntl.LOCK_EX if exclusive else json_io.fcntl.LOCK_SH
                    json_io.fcntl.flock(descriptor, mode | (0 if blocking else json_io.fcntl.LOCK_NB))
                else:
                    _lock_windows_generation(descriptor, exclusive=exclusive, blocking=blocking)
            except BlockingIOError as error:
                raise RuntimeDatabaseDeferred("generation_busy") from error
        finally:
            handle.lock.release()
        if not json_io._descriptor_matches_lock_path(descriptor, lock):
            raise RuntimeDatabaseDeferred("generation_lock_changed")
        if exclusive:
            state = [path, owner, descriptor, True]
            token = _EXCLUSIVE.set(state)
        elif runtime_db_recovery_pending(path):
            raise RuntimeDatabaseDeferred("recovery_pending")
        yield
        completed = True
    finally:
        if token is not None:
            state[3] = False
            _EXCLUSIVE.reset(token)
        try:
            if exclusive and completed:
                intent = _intent_path(path)
                with json_io.locked_json_path(intent, blocking=False):
                    intent.unlink(missing_ok=True)
        finally:
            if descriptor is not None:
                # 不用 LOCK_UN：fork 或 EX 内子连接仍可能合法持有同一 OFD。
                os.close(descriptor)


def _records():
    if not hasattr(_CONNECTIONS, "by_id"):
        _CONNECTIONS.by_id, _CONNECTIONS.by_path = {}, {}
    return _CONNECTIONS.by_id, _CONNECTIONS.by_path


def guarded_runtime_connect(path, connect):
    """给真实物理连接登记门寿命；调用方须经 close_runtime_connection 或跟踪连接的 close 归还。"""
    guard = runtime_db_generation_guard(path)
    guard.__enter__()
    try:
        conn = connect()
    except BaseException:
        guard.__exit__(None, None, None)
        raise
    by_id, by_path = _records()
    existing = by_id.get(id(conn))
    if existing is not None:
        guard.__exit__(None, None, None)
        if existing["path"] != _path(path) or existing["pid"] != os.getpid():
            raise RuntimeDatabaseDeferred("connection_generation_conflict")
        return conn
    record = dict(conn=conn, path=_path(path), guard=guard, pid=os.getpid(),
                  thread=threading.current_thread(), idle=False, detach=None)
    by_id[id(conn)] = record
    by_path.setdefault(record["path"], {})[id(conn)] = record
    # 线程退出不是物理关闭证明；未确认的连接与门保留到显式 close 或进程死亡。
    with _HELD_LOCK:
        _HELD_CONNECTIONS[(record["pid"], id(conn))] = record
    return conn


def mark_runtime_connection_idle(conn, idle, detach=None):
    record = _records()[0].get(id(conn))
    if record is not None:
        record["idle"] = idle
        if detach is not None:
            record["detach"] = detach


def release_closed_runtime_connection(conn):
    """只由创建线程确认 C 层连接已关闭；close 抛错前未关闭时保留 SH 和强引用。"""
    by_id, by_path = _records()
    record = by_id.get(id(conn))
    if record is None:
        return True
    if record["pid"] != os.getpid() or record["thread"] is not threading.current_thread():
        return False
    try:
        sqlite3.Connection.in_transaction.__get__(conn)
    except sqlite3.ProgrammingError:
        by_id.pop(id(conn))
        group = by_path[record["path"]]
        group.pop(id(conn))
        if not group:
            by_path.pop(record["path"])
        with _HELD_LOCK:
            _HELD_CONNECTIONS.pop((record["pid"], id(conn)), None)
        record["guard"].__exit__(None, None, None)
        return True
    return False


def close_runtime_connection(conn):
    try:
        conn.close()
    except BaseException:
        release_closed_runtime_connection(conn)
        raise
    if not release_closed_runtime_connection(conn):
        raise RuntimeDatabaseDeferred("physical_close_unconfirmed")


def record_runtime_corruption(conn):
    record = _records()[0].get(id(conn))
    if record is not None and record["pid"] == os.getpid() and record["thread"] is threading.current_thread():
        request_runtime_db_recovery(record["path"])


def retire_idle_runtime_connections(path):
    """只遍历本线程、该库的活连接；True 只表示本地排空，跨进程证明仍须 EX。"""
    group = _records()[1].get(_path(path), {})
    for record in tuple(group.values()):
        if record["pid"] != os.getpid() or record["thread"] is not threading.current_thread() or not record["idle"]:
            continue
        if record["detach"] is not None:
            record["detach"]()
        try:
            close_runtime_connection(record["conn"])
        except Exception:
            pass  # 未确认关闭的记录继续持门，下一次原恢复入口可重试。
    return not group
