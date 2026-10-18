# LLM: 这里只运输受信组合根的原同步短提交回调，不授角色/管理员/claim/pin权限；scope退出、跨PID/thread拒绝，默认路径纯内存无新增I/O。
# 模块用途: 让实际全局叶子RMW在资源锁前进入当前原写门，嵌套同次写复用活窗口，不把长业务放进生命周期锁。
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

from .global_write_guard import require_global_writer


# LLM: 错误仅说明原同步来源不再可消费，不重试、不授新来源，不回显路径/正文。
# 类用途: 拒绝已经退出或搬到另一个执行者的全局写上下文。
class GlobalWriteCommitUnavailable(RuntimeError):
    error_code = 'GLOBAL_WRITE_REJECTED'


# LLM: 原callback是实际C组合根的最终门；active只限制运输寿命，不证明权限，必须原callback每次核真实事实。
# 类用途: 记住当前同步调用的原提交器和创建执行者，退出后所有copy_context副本同时失效。
class _CommitScope:
    # LLM: 构造只读PID/thread和保存callback，canonical window只在原callback内绑定；无账本/锁/I/O，不从报文恢复。
    # 函数用途: 建立一段原同步调用的内存运输区间。
    def __init__(self, commit):
        self.commit, self.pid, self.thread = commit, os.getpid(), threading.get_ident()
        self.active, self.origin = True, self
        self.window = None

    # LLM: 同对象/原PID/thread与尚活区间全部必须匹配；即使原C仍存在也不替旧scope换新来源。
    # 函数用途: 拒绝复制、fork、跨线程或终态上下文的后续写入。
    def require_original(self):
        if (self.origin is not self or not self.active or self.pid != os.getpid()
                or self.thread != threading.get_ident()):
            raise GlobalWriteCommitUnavailable('original_global_write_scope_unavailable')


_SCOPE: ContextVar[_CommitScope | None] = ContextVar('original_global_write_commit', default=None)
# LLM: 短窗口与长scope各有独立finally寿命；copy_context不能在短门结束后借尚活scope绕原最后检查。
# 类用途: 标记此刻真正持有原最后写门的同源同步区间，门退出立即失效。
class _CommitWindow:
    # LLM: window只在原commit callback内创建；原对象印记和scope canonical指针共同限制copy/构造替身，tag/pin/JSON不授信。
    # 函数用途: 保存正在实际短提交的原scope。
    def __init__(self, scope):
        self.scope, self.active, self.origin = scope, True, self

    # LLM: 原window对象、同scope canonical指针和活短门必须同时成立；复制active=True或构造替身不能借尚活scope授权。
    # 函数用途: 拒绝跨源及复制的过期短窗口。
    def require_original(self, scope):
        scope.require_original()
        if (self.origin is not self or self.scope is not scope or not self.active
                or scope.window is not self):
            raise GlobalWriteCommitUnavailable('original_global_write_window_unavailable')


_WINDOW: ContextVar[_CommitWindow | None] = ContextVar('original_global_write_window', default=None)


# LLM: 仅受信C组合根绑定实际最后提交函数；嵌套不同源独立运输，finally使当前区间及其所有副本失效，不把pin当许可。
# 函数用途: 在一次原控制调用中把真正短写门传到原配置叶子。
@contextmanager
def global_write_commit_scope(commit: Callable):
    if not callable(commit):
        raise GlobalWriteCommitUnavailable('original_global_write_source_required')
    scope = _CommitScope(commit)
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _SCOPE.reset(token)


# LLM: 默认无source保持原调用；实际工具claim仅运输到叶子线程才构造短scope。已安装scope须由原callback核C/请求，嵌套仅同原活窗口不重拿T。
# LLM: require_scope仅给显式C本地IO拒绝无源回落，不创建第二scope；默认False保原工具/Count0分派。
# 函数用途: 在真正配置或C本地末写前消费原同步门，显式C缺源即拒绝，长handler不持门。
def execute_global_write(operation: Callable, *, require_scope: bool = False):
    require_global_writer('global_config')
    scope = _SCOPE.get()
    if scope is None:
        if require_scope:
            raise GlobalWriteCommitUnavailable('original_global_write_scope_required')
        from .tool_write_context import original_tool_write_commit

        commit = original_tool_write_commit()
        if commit is None:
            return operation()
        with global_write_commit_scope(commit):
            return execute_global_write(operation)
    if type(scope) is not _CommitScope:
        raise GlobalWriteCommitUnavailable('original_global_write_scope_unavailable')
    scope.require_original()
    window = _WINDOW.get()
    if window is not None:
        if type(window) is not _CommitWindow:
            raise GlobalWriteCommitUnavailable('original_global_write_window_unavailable')
        window.require_original(scope)
        return operation()

    # LLM: 只有原commit最后门调用此callback才能设窗口；窗口退出立即还原，不能缓存到异步/后续请求使用。
    # 函数用途: 在原C已核实且持短门时执行一次同步RMW及其合法嵌套。
    def committed():
        scope.require_original()
        if scope.window is not None:
            raise GlobalWriteCommitUnavailable('original_global_write_window_conflict')
        window = _CommitWindow(scope)
        scope.window = window
        token = _WINDOW.set(window)
        try:
            return operation()
        finally:
            window.active = False
            scope.window = None
            _WINDOW.reset(token)

    return scope.commit(committed)
