"""owner 激活代次；只有激活事务修改 metadata，不替代执行退出证明。"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_ACTIVATION_LOCK = threading.Lock()
_ACTIVATED: dict[tuple[int, str], int] = {}
_WRITE_CONTEXT: ContextVar[tuple | None] = ContextVar("owner_epoch_write_context", default=None)


class StaleOwnerEpoch(RuntimeError):
    code = "STALE_OWNER_EPOCH"
    error_code = "STALE_OWNER_EPOCH"

    def __init__(self, expected: int, current: int):
        self.expected, self.current = expected, current
        super().__init__(self.code)


def read_owner_epoch(repo) -> int:
    with repo.transaction() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'owner_epoch'").fetchone()
        return int(row[0]) if row is not None else 0


def activate_owner_epoch(repo) -> int:
    """BEGIN IMMEDIATE 的同一提交点分配唯一连续代次，不更改 schema。"""
    from ..concurrency.lock_order import LockLevel, note_acquired, note_released

    with repo.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        note_acquired(LockLevel.SQLITE, id(conn), try_acquire=False)
        try:
            conn.execute("INSERT INTO metadata(key, value) VALUES('owner_epoch', '1') "
                         "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            value = int(conn.execute("SELECT value FROM metadata WHERE key = 'owner_epoch'").fetchone()[0])
        finally:
            note_released(id(conn))
    return value


def ensure_process_owner_epoch_activated(repo) -> int:
    """集合按进程与规范DB路径去重，并保留本执行域最初的代次而非重读新代次。"""
    key = (os.getpid(), str(Path(repo.db_path).resolve()))
    with _ACTIVATION_LOCK:
        if key not in _ACTIVATED:
            _ACTIVATED[key] = activate_owner_epoch(repo)
        return _ACTIVATED[key]


def require_current(repo, expected: int) -> None:
    current = read_owner_epoch(repo)
    if current != expected:
        raise StaleOwnerEpoch(expected, current)


def reset_process_activations_for_tests() -> None:
    """仅模拟新进程；正常缓存重建不得调用。"""
    with _ACTIVATION_LOCK:
        _ACTIVATED.clear()


@contextmanager
def request_epoch_scope(repo, expected: int, config):
    token = _WRITE_CONTEXT.set((repo, expected, config))
    try:
        yield
    finally:
        _WRITE_CONTEXT.reset(token)


def current_request_epoch():
    return _WRITE_CONTEXT.get()


def check_store_terminal_epoch(storage, expected_owner_epoch: int | None) -> None:
    """在原 Store 锁内调用；配置保持活引用，每次终态提交独立读取开关。"""
    context = _WRITE_CONTEXT.get()
    repo = getattr(storage, "owner_epoch_repository", None)
    config = getattr(storage, "owner_epoch_config", None)
    if context is not None and (repo is None or Path(repo.db_path).resolve() == Path(context[0].db_path).resolve()):
        repo, captured, config = context
        if expected_owner_epoch is None:
            expected_owner_epoch = captured
    if expected_owner_epoch is not None and getattr(config, "owner_epoch_fence_enabled", False):
        if repo is None:
            raise RuntimeError("owner epoch repository is required for a fenced terminal write")
        require_current(repo, expected_owner_epoch)
