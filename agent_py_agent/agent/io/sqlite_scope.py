# LLM: SQLite缓存与非缓存连接都由创建者作用域回收；退出后复制Context不得复活缓存；异地归还只失效租期，不解析owner或授权。
# 模块用途: 提供请求级连接上下文，作用域外保持原短连接行为。

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path

from .sqlite_connection import TrackedSQLiteConnection
from .sqlite_lease import SQLiteConnectionLease

# 一个 WAL 库约三个句柄；每作用域八条仅限制闲置缓存，不是进程上限，也不限制在用的嵌套短连接。
_SQLITE_SCOPE_MAX_CONNECTIONS_COUNT = 8


# LLM: 每项只持有一条物理连接及打开时文件身份；在用时绝不能再次借出，避免内层提交外层事务。
# 类用途: 记录缓存连接是否空闲，以及它实际对应的库文件。
@dataclass
class _ConnectionEntry:
    conn: sqlite3.Connection
    file_identity: tuple[int, int]
    in_use: bool = True


# LLM: 复制Context不转移PID/Thread所有权或延长寿命；closed由创建者结束时永久置位，entries/uncached都由原scope回收。
# 类用途: 只在一个请求的创建线程里保存少量连接，不是永久连接池。
@dataclass
class _ConnectionScope:
    pid: int = field(default_factory=os.getpid)
    thread_id: int = field(default_factory=threading.get_ident)
    thread: threading.Thread = field(default_factory=threading.current_thread)
    closed: bool = field(default=False, init=False)
    entries: dict[tuple[int, int, Path, bool], _ConnectionEntry] = field(default_factory=dict)
    uncached: dict[int, sqlite3.Connection] = field(default_factory=dict, init=False)


_SQLITE_ACTIVE_SCOPE: ContextVar[_ConnectionScope | None] = ContextVar("sqlite_connection_scope", default=None)


# LLM: PID、Thread对象与closed共同证明作用域有效；fork、线程标识复用或复制的过期Context都不能访问缓存。
# 函数用途: 判断当前执行者是不是这个连接作用域的真正创建者。
def _scope_owned(scope: _ConnectionScope) -> bool:
    return (not scope.closed and scope.pid == os.getpid() and scope.thread_id == threading.get_ident()
            and scope.thread is threading.current_thread())


# LLM: 未启用/跨线程/fork 保持标准库连接；可复用连接才跟踪游标，工厂不决定业务权限。
# 函数用途: 让原连接入口选择具有语句清理能力的标准库子类。
def sqlite_connection_factory() -> type[sqlite3.Connection]:
    scope = _SQLITE_ACTIVE_SCOPE.get()
    return TrackedSQLiteConnection if scope is not None and _scope_owned(scope) else sqlite3.Connection


# LLM: 每次借用前从文件系统读取身份，文件不见/不可读不能当作旧连接仍可用；不缓存 stat 结果。
# 函数用途: 取得库文件的设备号和 inode，供替换/清理后的连接失效判定。
def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    except OSError:
        return None


# LLM: 单项退役的普通 close 错误继续沿旧语义忽略；中断向上传播，全 scope 关闭另行收集全部 BaseException。
# 函数用途: 安全尝试关闭已退役的连接。
def _close_connection(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 清理失败不得阻止其它连接回收
        pass


# LLM: 有业务/先前清理异常时继续完成清理但不替换原异常对象；无原异常的中断必须传播，不能吞停止。
# 函数用途: 执行清理，并保持异常发生的先后顺序。
def _cleanup_preserving_error(cleanup: Callable[[], None], original: BaseException | None) -> None:
    try:
        cleanup()
    except BaseException:
        if original is None:
            raise


# LLM: 先摘缓存后关闭，归还失败/文件换代都不能再次借出此项；无数据库业务写入。
# 函数用途: 退役一个失效或清理失败的缓存连接。
def _discard_entry(scope: _ConnectionScope, key: tuple[int, int, Path, bool]) -> None:
    entry = scope.entries.pop(key)
    _close_connection(entry.conn)


# LLM: 库在同一作用域内从另一条路径移来时，源路径旧 WAL 连接也须先退出；只清理空闲项，不碰在用事务。
# 函数用途: 新开连接前退役其它已移动/替换的缓存，避免同一 inode 同时使用两个 WAL 文件名。
def _retire_stale_entries(scope: _ConnectionScope) -> None:
    for key, entry in tuple(scope.entries.items()):
        if not entry.in_use and _file_identity(key[2]) != entry.file_identity:
            _discard_entry(scope, key)


# LLM: 只借空闲且 inode 未变的项；新连接归原工厂设 PRAGMA，容量满或同库正在用时退回短连接。
# 函数用途: 找到或建立本作用域可独占借用的缓存项。
def _acquire_entry(scope: _ConnectionScope, key: tuple[int, int, Path, bool], connect: Callable[[], sqlite3.Connection]) -> _ConnectionEntry | None:
    entry = scope.entries.get(key)
    if entry is not None and entry.in_use:
        return None
    if entry is not None and _file_identity(key[2]) == entry.file_identity:
        entry.in_use = True
        return entry
    if entry is not None:
        _discard_entry(scope, key)
    _retire_stale_entries(scope)
    if len(scope.entries) >= _SQLITE_SCOPE_MAX_CONNECTIONS_COUNT:
        return None
    before = _file_identity(key[2])
    conn = connect()
    after = _file_identity(key[2])
    if after is None or (before is not None and before != after):
        _close_connection(conn)
        return None
    entry = _ConnectionEntry(conn, after)
    scope.entries[key] = entry
    return entry


# LLM: 归还也先核 PID/Thread；非创建者只结束本地借用，不能操作继承连接或把创建者缓存改为空闲。
#   原线程正常归还不提交，先前中断不能被退役 close 的后续中断覆盖。
# 函数用途: 回滚遗留事务后标为空闲，失败连接永不复用。
def _release_entry(scope: _ConnectionScope, key: tuple[int, int, Path, bool], entry: _ConnectionEntry) -> None:
    if not _scope_owned(scope):
        return
    try:
        if entry.conn.in_transaction:
            entry.conn.rollback()
        if not isinstance(entry.conn, TrackedSQLiteConnection):
            _discard_entry(scope, key)
            return
        entry.conn.close_lease_cursors()
        if _file_identity(key[2]) != entry.file_identity:
            _discard_entry(scope, key)
            return
        entry.in_use = False
    except Exception:  # noqa: BLE001 退役坏连接，不覆盖原业务结果
        _discard_entry(scope, key)
    except BaseException as exc:
        _cleanup_preserving_error(lambda: _discard_entry(scope, key), exc)
        raise


# LLM: 只由创建者清理；先永久关闭scope并摘除全部登记，再记首个BaseException继续关闭，使未归还lease也立刻过期且不提交。
# 函数用途: 请求结束时回收本次所有连接，包括在其它线程结束租期但尚未关闭的短连接。
def _close_scope(scope: _ConnectionScope) -> None:
    if not _scope_owned(scope):
        return
    scope.closed = True
    connections = tuple(entry.conn for entry in scope.entries.values()) + tuple(scope.uncached.values())
    scope.entries.clear()
    scope.uncached.clear()
    first_error = None
    for conn in connections:
        try:
            conn.close()
        except BaseException as exc:
            first_error = exc if first_error is None else first_error
    if first_error is not None:
        raise first_error


# LLM: 即使清理被中断也必须还原外层 Context，避免后续请求误继承已退役作用域。
# 函数用途: 关闭本次缓存，并无条件恢复进入前的作用域绑定。
def _finish_scope(scope: _ConnectionScope | None, token: Token) -> None:
    try:
        if scope is not None:
            _close_scope(scope)
    finally:
        _SQLITE_ACTIVE_SCOPE.reset(token)


# LLM: 显式 enabled 是唯一启用事实；无条件恢复 Context，已有业务异常不能被关闭中断替换，默认不缓存。
# 函数用途: 为一个请求声明连接寿命边界。
@contextmanager
def sqlite_connection_scope(enabled: bool = False) -> Iterator[None]:
    scope = _ConnectionScope() if enabled else None
    token = _SQLITE_ACTIVE_SCOPE.set(scope)
    body_error = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _cleanup_preserving_error(lambda: _finish_scope(scope, token), body_error)


# LLM: 未启用、容量满、同库在用或跨线程均由原工厂开关连接；不自动提交业务事务。
# 函数用途: 管理不能缓存的单次短连接。
@contextmanager
def _short_connection(connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# LLM: 租期在归还前失效，行工厂仅原创建者恢复；失效清理不覆盖业务异常，不执行SQL。
# 函数用途: 给一次启用复用的借用建立并结束独立访问身份。
@contextmanager
def _leased_connection(conn: sqlite3.Connection, scope: _ConnectionScope) -> Iterator[SQLiteConnectionLease]:
    lease = SQLiteConnectionLease(conn, lambda: _scope_owned(scope))
    body_error = None
    try:
        yield lease
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _cleanup_preserving_error(lease._invalidate, body_error)


# LLM: 非创建者不摘原线程登记、不维护SQLite；归还租期已失效，物理回收留给创建者退出scope。原线程先摘后关避免重复回收。
# 函数用途: 原线程立即关闭正常归还的非缓存连接，异地归还保持创建者的回收记录。
def _release_uncached_connection(scope: _ConnectionScope, conn: sqlite3.Connection) -> None:
    if not _scope_owned(scope):
        return
    if scope.uncached.pop(id(conn), None) is not None:
        conn.close()


# LLM: 嵌套/溢出不是缓存但仍归创建者scope管理；交租期前登记强引用，异地只失效访问层，关闭中断不覆盖原异常。
# 函数用途: 管理不能复用的短连接，防止异地归还后遗留写锁；默认关闭分支不走此入口。
@contextmanager
def _uncached_scope_connection(scope: _ConnectionScope, connect: Callable[[], sqlite3.Connection]) -> Iterator[SQLiteConnectionLease]:
    conn = connect()
    scope.uncached[id(conn)] = conn
    body_error = None
    try:
        with _leased_connection(conn, scope) as lease:
            yield lease
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _cleanup_preserving_error(lambda: _release_uncached_connection(scope, conn), body_error)


# LLM: 启用时缓存和溢出连接都包租期，缓存归还先失效且不覆盖业务异常，默认关闭短连接不改变。
# 函数用途: 先结束访问层，再归还物理连接或关闭溢出的短连接。
@contextmanager
def _scope_connection(scope: _ConnectionScope, key: tuple[int, int, Path, bool], connect: Callable[[], sqlite3.Connection]) -> Iterator[SQLiteConnectionLease]:
    entry = _acquire_entry(scope, key, connect)
    if entry is None:
        with _uncached_scope_connection(scope, connect) as lease:
            yield lease
        return
    body_error = None
    try:
        with _leased_connection(entry.conn, scope) as lease:
            yield lease
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _cleanup_preserving_error(lambda: _release_entry(scope, key, entry), body_error)


# LLM: 调用方工厂负责访问模式和 PRAGMA；完整身份键仅在原进程/线程的显式作用域内使用。
# 函数用途: 为数据库操作选择请求级缓存或旧短连接寿命。
@contextmanager
def scoped_connection(path: Path, read_only: bool, connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    scope = _SQLITE_ACTIVE_SCOPE.get()
    if scope is not None and _scope_owned(scope):
        key = scope.pid, scope.thread_id, Path(os.path.abspath(path)), read_only
        with _scope_connection(scope, key, connect) as conn:
            yield conn
        return
    with _short_connection(connect) as conn:
        yield conn
