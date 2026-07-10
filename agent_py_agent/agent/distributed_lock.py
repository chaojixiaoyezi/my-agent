"""分布式锁(Tier 0.3 企业规模化):多实例协调。

自建取舍(用户原则"能自建就自建"):PostgreSQL advisory lock 就是两句 SQL
(``pg_advisory_lock`` / ``pg_try_advisory_lock`` / ``pg_advisory_unlock``),薄封装**自建**,
不引 Redis(已上 PG 就复用)。现状的文件锁 fcntl/flock 只跨进程、单机;**跨实例(水平扩展)
必须共享后端协调 → PG advisory lock**。

后端是 SQLite(本地/开发)时回退到进程内 ``threading.Lock``——诚实边界:SQLite 本就单机,
此回退只协调单进程多线程;真跨实例/跨进程分布式锁需 PostgreSQL 后端。
"""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from agent_py_agent.agent.storage_backend import StorageBackend


def key_to_lock_id(key: str) -> int:
    """字符串键 → PG advisory lock 用的有符号 bigint(sha256 取前 8 字节)。"""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big", signed=True)


def _release_if(lock: threading.Lock, got: bool) -> None:
    if got:
        lock.release()


class DistributedLock:
    """跨实例互斥锁。PG=advisory lock(真分布式);SQLite=本地 threading.Lock(单进程回退)。"""

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend
        self._local: dict[str, threading.Lock] = defaultdict(threading.Lock)

    @contextmanager
    def acquire(self, key: str) -> Iterator[bool]:
        """阻塞获取(成功 yield True)。PG=pg_advisory_lock 跨实例;SQLite=threading.Lock 单进程。"""
        if not self._backend.is_postgres:
            with self._local[key]:
                yield True
            return
        yield from self._pg_session(key, blocking=True)

    @contextmanager
    def try_acquire(self, key: str) -> Iterator[bool]:
        """非阻塞:拿到 yield True、没拿到 yield False(立即返回,不等)。"""
        if self._backend.is_postgres:
            yield from self._pg_session(key, blocking=False)
        else:
            yield from self._local_try(key)

    def _local_try(self, key: str) -> Iterator[bool]:
        lk = self._local[key]
        got = lk.acquire(blocking=False)
        try:
            yield got
        finally:
            _release_if(lk, got)

    def _pg_session(self, key: str, *, blocking: bool) -> Iterator[bool]:
        from sqlalchemy import text

        lock_id = key_to_lock_id(key)
        conn = self._backend.engine.connect()
        acquired = False
        try:
            if blocking:
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_id})
                acquired = True
            else:
                acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_id}).scalar())
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_id})
            conn.close()  # 还连接进池(advisory lock 是 session 级,随连接释放也会清,显式 unlock 更稳)
