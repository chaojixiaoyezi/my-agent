"""owner 激活代次；只有激活事务修改 metadata，不替代执行退出证明。"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..io.sqlite_scope import _file_identity

_ACTIVATION_LOCK = threading.Lock()
_ACTIVATED: dict[tuple[int, str], tuple[tuple[int, int] | None, int]] = {}
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


# LLM: 显式激活者在原写事务内分配高于持久值和 minimum 的代次；不更改 schema。
# 函数用途: 给新执行域或重建后的库分配不倒退的 epoch。
def activate_owner_epoch(repo, *, minimum: int = 0) -> int:
    """BEGIN IMMEDIATE 的同一提交点分配 max(current_epoch, minimum) + 1。"""
    from ..concurrency.lock_order import LockLevel, note_acquired, note_released

    with repo.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        note_acquired(LockLevel.SQLITE, id(conn), try_acquire=False)
        try:
            conn.execute("INSERT INTO metadata(key, value) VALUES('owner_epoch', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), ?) + 1",
                         (max(0, minimum) + 1, minimum))
            value = int(conn.execute("SELECT value FROM metadata WHERE key = 'owner_epoch'").fetchone()[0])
        finally:
            note_released(id(conn))
    return value


# LLM: 缓存只因物理换库或零 epoch 重新激活；原库的非零新代次不能授给旧执行域。
# 函数用途: 复用本进程的激活代次，库自愈后则跨过旧代号。
def ensure_process_owner_epoch_activated(repo) -> int:
    """按进程、规范路径和物理库关联原代次；零 epoch 兼容 inode 复用。"""
    path = Path(repo.db_path).resolve()
    key = (os.getpid(), str(path))
    with _ACTIVATION_LOCK:
        cached = _ACTIVATED.get(key)
        if cached is None or cached[0] != _file_identity(path) or read_owner_epoch(repo) == 0:
            value = activate_owner_epoch(repo, minimum=cached[1] if cached else 0)
            # 激活事务可能刚刚重建缺失的库，因此在提交后采样物理身份。
            _ACTIVATED[key] = (_file_identity(path), value)
        return _ACTIVATED[key][1]


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
