# LLM: 仅为Gateway单线程规划延长原io作用域寿命，沿原PID/Thread、租期、容量和文件换代规则；不创建第二连接池。
# 模块用途: 在允许的runtime_db入口提供跨拍SQLite作用域，原请求级作用域行为不变。
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..io.sqlite_scope import (
    _SQLITE_ACTIVE_SCOPE,
    _cleanup_preserving_error,
    _close_scope,
    _ConnectionScope,
    _discard_entry,
    _scope_owned,
)


# LLM: 仅扩展原连接表的激活计数，不另建连接池；计数不放Context，避免临时遮蔽或复制Context误判嵌套。
# 类用途: 记录一个持久连接表尚未退出的激活层数。
@dataclass
class _PersistentConnectionScope(_ConnectionScope):
    _activation_depth: int = field(default=0, init=False, repr=False, compare=False)


# LLM: 仅在真正执行规划的线程惰性创建；构造不连接数据库，原scope继续负责PID和Thread所有权。
# 函数用途: 创建一个由后台规划者显式关闭的持久连接作用域。
def new_persistent_sqlite_scope() -> _PersistentConnectionScope:
    return _PersistentConnectionScope()


# LLM: 最外层规划边界若仍有未归还/异地归还连接，按原scope整体退役以回滚并使全部lease失效，下一拍由Gateway重建。
#   正常只提交已归还的闲置连接；不能提交外层仍在用事务，失败项退役后继续清理再传播首错。
# 函数用途: 释放规划连接的遗留读快照，避免持久连接阻塞WAL检查点。
def _commit_planner_connections(scope: _ConnectionScope) -> None:
    if not _scope_owned(scope):
        return
    if scope.uncached or any(entry.in_use for entry in scope.entries.values()):
        close_persistent_sqlite_scope(scope)
        return
    first_error = None
    for key, entry in tuple(scope.entries.items()):
        if entry.in_use:
            continue
        try:
            entry.conn.commit()
        except BaseException as exc:
            _cleanup_preserving_error(lambda key=key: _discard_entry(scope, key), exc)
            first_error = exc if first_error is None else first_error
    if first_error is not None:
        raise first_error


# LLM: 只激活创建者的活scope；按表计数，真正最外层离开才提交闲置连接；始终还原Context且不覆盖业务异常。
# 函数用途: 跨多拍复用同一规划连接表，但不把它传给提交或模型执行车道。
@contextmanager
def activate_sqlite_scope(scope: _PersistentConnectionScope | None) -> Iterator[None]:
    if scope is not None and not _scope_owned(scope):
        raise RuntimeError("persistent SQLite scope is closed or belongs to another thread/process")
    if scope is not None:
        scope._activation_depth += 1
    token = _SQLITE_ACTIVE_SCOPE.set(scope)
    body_error = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            if scope is not None:
                scope._activation_depth -= 1
                if scope._activation_depth == 0:
                    _cleanup_preserving_error(lambda: _commit_planner_connections(scope), body_error)
        finally:
            _SQLITE_ACTIVE_SCOPE.reset(token)


# LLM: 物理关闭复用原创建者清理算法；幂等、fork/异线程不碰继承连接，过期lease由原closed标志拒绝。
# 函数用途: 在owner移除或规划线程停机时关闭全部持久连接。
def close_persistent_sqlite_scope(scope: _ConnectionScope) -> None:
    _close_scope(scope)
