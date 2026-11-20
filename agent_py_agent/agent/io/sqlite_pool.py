"""按线程复用已核验的 SQLite 连接，由进程级登记表回收闲置连接。"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakValueDictionary

from .sqlite_connection import TrackedSQLiteConnection

# 空闲达到此时长后即可由任意线程关闭连接，持续借用的连接不计入空闲。
SQLITE_POOL_IDLE_TIMEOUT_SECONDS = 60


@dataclass(eq=False)
class _PathState:
    path: Path
    writer: threading.RLock = field(default_factory=threading.RLock)
    generation: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def current_generation(self) -> int:
        with self.lock:
            return self.generation


@dataclass(eq=False)
class _Entry:
    state: _PathState
    conn: sqlite3.Connection
    identity: tuple
    generation: int
    leased: bool = True
    closed: bool = False
    idle_since: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.paths: WeakValueDictionary[Path, _PathState] = WeakValueDictionary()
        self.entries: dict[Path, set[_Entry]] = {}
        self.local = threading.local()
        self.next_retirement = float("-inf")

    def state(self, path: Path) -> _PathState:
        with self.lock:
            state = self.paths.get(path)
            if state is None:
                self.paths[path] = state = _PathState(path)
            return state

    def cache(self) -> WeakValueDictionary:
        if not hasattr(self.local, "entries"):
            self.local.entries = WeakValueDictionary()
        return self.local.entries

    def add(self, entry: _Entry) -> None:
        with self.lock:
            self.entries.setdefault(entry.state.path, set()).add(entry)

    def close(self, entry: _Entry) -> None:
        # 调用方已独占租期或已标记关闭。保留登记直到物理关闭完成。
        try:
            entry.conn.close()
        except BaseException:
            with entry.lock:
                entry.generation = -1
                entry.closed = False
            raise
        with self.lock:
            path = entry.state.path
            group = self.entries[path]
            group.remove(entry)
            if not group:
                del self.entries[path]

    def retire_idle(self, now: float, *, from_checkout: bool = False) -> int:
        with self.lock:
            if from_checkout:
                if now < self.next_retirement:
                    return 0
                self.next_retirement = now + SQLITE_POOL_IDLE_TIMEOUT_SECONDS
            entries = [entry for group in self.entries.values() for entry in group]
        retired = 0
        for entry in entries:
            with entry.lock:
                if entry.closed or entry.leased or now - entry.idle_since < SQLITE_POOL_IDLE_TIMEOUT_SECONDS:
                    continue
                entry.closed = True
            self.close(entry)
            retired += 1
        return retired

    def invalidate(self, state: _PathState) -> int:
        with state.lock:
            state.generation += 1
            generation = state.generation
        with self.lock:
            entries = tuple(self.entries.get(state.path, ()))
        for entry in entries:
            with entry.lock:
                if entry.closed or entry.leased or entry.generation >= generation:
                    continue
                entry.closed = True
            self.close(entry)
        return generation


_REGISTRY = _Registry()
_INHERITED_REGISTRIES: list[_Registry] = []


def _after_fork() -> None:
    global _REGISTRY
    # 子进程不借用继承的 SQLite 连接或锁，也不从 fork 回调调用 SQLite C 接口。
    _INHERITED_REGISTRIES.append(_REGISTRY)
    _REGISTRY = _Registry()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _now() -> float:
    return time.monotonic()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def invalidate(path: Path) -> None:
    """使该库的连接换代。仍在租期内的连接由归还者关闭。"""
    registry = _REGISTRY
    with registry.lock:
        state = registry.paths.get(_absolute(path))
    if state is not None:
        registry.invalidate(state)


def retire_idle_connections(*, now: float | None = None) -> int:
    """回收达到空闲期限的物理连接，返回实际关闭数。"""
    return _REGISTRY.retire_idle(_now() if now is None else now)


class PooledSqlite:
    """工厂须使用 check_same_thread=False 和 cached_statements=0，允许异线程及时回收。"""

    def __init__(self, path: Path, *, verify_open: Callable[[], sqlite3.Connection],
                 ensure_schema: Callable[[sqlite3.Connection], None]) -> None:
        self.path = _absolute(path)
        self._verify_open = verify_open
        self._ensure_schema = ensure_schema
        self._cache_kind = object()
        self._binding = _REGISTRY, _REGISTRY.state(self.path)

    def _state(self) -> tuple[_Registry, _PathState]:
        registry, state = self._binding
        if registry is not _REGISTRY:
            registry = _REGISTRY
            state = registry.state(self.path)
            self._binding = registry, state
        return registry, state

    def bump_generation(self) -> int:
        registry, state = self._state()
        return registry.invalidate(state)

    def _identity(self) -> tuple | None:
        try:
            root, db = self.path.parent.lstat(), self.path.lstat()
            if stat.S_ISDIR(root.st_mode) and stat.S_ISREG(db.st_mode) and db.st_nlink == 1:
                return self._fingerprint(root, db)
        except OSError:
            # 轻量比较失败后，冷开负责复核并保留具体的文件错误。
            return None
        return None

    def _fingerprint(self, root: os.stat_result, db: os.stat_result) -> tuple | None:
        return (root.st_dev, root.st_ino), (db.st_dev, db.st_ino)

    def _open_guard(self, state: _PathState):
        return nullcontext()

    def _cached_schema_current(self, db: sqlite3.Connection) -> bool:
        return True

    def _initialize(self, db: sqlite3.Connection) -> bool:
        self._ensure_schema(db)
        return False

    def _validate_before_return(self, db: sqlite3.Connection, identity: tuple) -> None:
        pass

    def _open(self, registry: _Registry, state: _PathState) -> _Entry:
        with self._open_guard(state):
            generation = state.current_generation()
            before = self._identity()
            conn = self._verify_open()
            try:
                identity = self._identity()
                if identity is None or (before is not None and before != identity):
                    raise OSError("SQLite file or directory identity changed during open")
                with conn:
                    migrated = self._initialize(conn)
                    self._validate_before_return(conn, identity)
                if migrated:
                    generation = registry.invalidate(state)
                with state.lock:
                    if generation != state.generation:
                        raise sqlite3.OperationalError("SQLite pool generation changed during open")
                    entry = _Entry(state, conn, identity, generation)
                    registry.add(entry)
                return entry
            except BaseException:
                conn.close()
                raise

    def _acquire(self, registry: _Registry, state: _PathState) -> tuple[_Entry, bool]:
        cache = registry.cache()
        key = self.path, self._cache_kind
        entry = cache.get(key)
        cacheable = True
        if entry is not None:
            with entry.lock:
                cacheable = not entry.leased
                reusable = (cacheable and not entry.closed and entry.identity == self._identity()
                            and entry.generation == state.current_generation())
                if reusable:
                    entry.leased = True
                retire = cacheable and not reusable and not entry.closed
                if retire:
                    entry.closed = True
            if reusable:
                try:
                    if self._cached_schema_current(entry.conn):
                        return entry, True
                except BaseException:
                    self._release(registry, entry, keep=False)
                    raise
                self._release(registry, entry, keep=False)
            elif retire:
                registry.close(entry)
        entry = self._open(registry, state)
        if cacheable:
            cache[key] = entry
        return entry, cacheable

    @staticmethod
    def _release(registry: _Registry, entry: _Entry, *, keep: bool) -> None:
        with entry.lock:
            entry.leased = False
            entry.idle_since = _now()
            entry.closed = not keep or entry.generation != entry.state.current_generation()
            closed = entry.closed
        if closed:
            registry.close(entry)

    @contextmanager
    def checkout(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        registry, state = self._state()
        now = _now()
        if now >= registry.next_retirement:
            registry.retire_idle(now, from_checkout=True)
        with state.writer if write else nullcontext():
            entry, cacheable = self._acquire(registry, state)
            db = entry.conn
            keep = False
            try:
                with db:
                    if write:
                        self._validate_before_return(db, entry.identity)
                        db.execute("PRAGMA busy_timeout=5000").close()
                        db.execute("BEGIN IMMEDIATE").close()
                    yield db
                    self._validate_before_return(db, entry.identity)
                if isinstance(db, TrackedSQLiteConnection):
                    db.close_lease_cursors()
                keep = cacheable
            finally:
                self._release(registry, entry, keep=keep)
