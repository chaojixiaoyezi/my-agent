# LLM: 真实请求invocation/BG/child组合root只捕获同execution/claim/lifetime；原T/P3/P5最后门仍唯一授权，公共业务只消费common端口，同步失端口/foreground/child测试。
# 模块用途: 向当前实际请求运输原平台短事务回调，不持门等待模型或线程。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    request_execution_ports_scope,
)
from .multiprocess_execution import WorkerExecution, current_worker_execution

_FOREGROUND_WINDOW = ContextVar("original_foreground_write_window", default=None)


# LLM: source只保存原execution/transition及可选BG对象，不能从当前claim代次补捕获；自己的window仅运输原T持锁区间，不是第二执行账。
# 类用途: 给公共文件事务提供同一个原P3/P5/T短门。
class OriginalRequestWriteSource:
    # LLM: 原执行已由invocation确认，构造纯内存，不读文件、创建claim或授权业务。
    # 函数用途: 保存实际请求及当前平台来源。
    def __init__(self, execution, background):
        from .multiprocess_coordinator_execution import CoordinatorExecution
        from .request_binding import GatewayActiveTurnTransition

        claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
        self.execution, self.background = execution, background
        self._execution_origin, self._background_origin = execution, background
        self.transition = (background.transition if background is not None else
                           GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id))
        self._transition_origin = self.transition
        self._origin, self._creator_pid, self._active = self, os.getpid(), True
        self._window = None

    # LLM: 拒绝使用捕获原claim，不从fresh文件/角色/正文猜身份；即使scope已退出仍可交精确欠账事件。
    # 函数用途: 构造这次短操作未进入的原typed拒绝。
    def rejection(self, phase):
        from .multiprocess_background_execution import _uncertain_execution

        return _uncertain_execution(self._transition_origin, phase)

    # LLM: 只核实际current execution/原lifetime与BG指针；最终T内仍独立核原claim、P3、C生命周期，活性自身不授写权。
    # 函数用途: 拒绝换源、复制、退出、fork和跨请求端口。
    def require_original(self, phase):
        from .multiprocess_background_execution import (
            _original_background_lifetime_active,
            current_background_request,
        )

        if (type(self) is not OriginalRequestWriteSource or self._origin is not self or not self._active
                or self.execution is not self._execution_origin or self.background is not self._background_origin
                or self.transition is not self._transition_origin
                or self._creator_pid != os.getpid() or current_worker_execution() is not self.execution):
            raise self.rejection(phase)
        try:
            self.execution.lifetime.require_original(self.execution)
        except (AttributeError, ValueError) as error:
            raise self.rejection(phase) from error
        if self.background is not None and (
                current_background_request() is not self.background
                or self.background.execution is not self.execution
                or not _original_background_lifetime_active(self.background.lifetime)):
            raise self.rejection(phase)
        if self.background is None and current_background_request() is not None:
            raise self.rejection(phase)

    # LLM: 只有此source在真实T里发布的原窗口可nested；同线程/PID/origin/transition准确，不据active字段绕门。
    # 函数用途: 核当前foreground是否真正持有原短门。
    def _holds_window(self):
        from .multiprocess_background_execution import BackgroundTransaction

        held = self._window
        return (_FOREGROUND_WINDOW.get() is held and type(held) is BackgroundTransaction and held._origin is held and held.active is True
                and held.transition is self.transition and held.creator_pid == os.getpid()
                and held.thread_id == threading.get_ident())

    # LLM: BG复用原共享window逻辑，foreground在同一个原transition.scope下完成短body；nested不重入非重入T，网络/线程等待必须外移。
    # 函数用途: 在平台最终门内执行一次公开文件事务。
    @contextmanager
    def short_scope(self, phase):
        from .multiprocess_background_execution import BackgroundTransaction, background_short_scope

        self.require_original(phase)
        if self.background is not None:
            with background_short_scope(phase, transition=self.transition):
                yield
            return
        if _FOREGROUND_WINDOW.get() is not None:
            if not self._holds_window():
                raise self.rejection(phase)
            yield
            return
        entered = False
        window = BackgroundTransaction(self.transition, os.getpid(), threading.get_ident())
        try:
            with self.transition.scope(phase):
                self.require_original(phase)
                entered = True
                self._window = window
                token = _FOREGROUND_WINDOW.set(window)
                try:
                    yield
                finally:
                    window.active = False
                    self._window = None
                    _FOREGROUND_WINDOW.reset(token)
        except Exception as error:
            if entered:
                raise
            raise self.rejection(phase) from error

    # LLM: 原ASK callback仍完整位于同一short_scope/T/P3/P5，不建立HCM或第二授权；inner共享原窗口。
    # 函数用途: 为公共文件元数据提交提供原请求的回调式短门。
    def short_operation(self, phase, operation):
        with self.short_scope(phase):
            return operation()

    # LLM: inner已持业务锁，只核原持T窗，不倒取T；BG仍用原lifetime/current window核，不认外层长scope替代短门。
    # 函数用途: 在真正内部落盘前确认外层短事务仍持原门。
    def require_writer(self, phase):
        from .multiprocess_background_execution import require_background_transaction

        self.require_original(phase)
        if self.background is not None:
            require_background_transaction(phase)
        elif not self._holds_window():
            raise self.rejection(phase)


# LLM: common安装/消费都经平台精确type/origin validator；FG/BG/HCM原算法不改，恢复只认当次P2/P3 metadata source且不授P5/模型/工具。
# 函数用途: 拒绝伪造或复制的来源；恢复source只可消费原issued元数据窗口，不是P5执行权。
def require_original_request_write_source(source, phase):
    from .multiprocess_host_command_scope import OriginalHostCommandWriteSource
    from .multiprocess_processing_recovery import OriginalProcessingRecoverySource

    if type(source) not in (OriginalRequestWriteSource, OriginalHostCommandWriteSource, OriginalProcessingRecoverySource):
        raise missing_request_context_rejection(phase)
    source.require_original(phase)


# LLM: missing只拒绝、不构造新cell或授信；有原current execution时保捕获claim的typed身份，无claim明确缺上下文，不读fresh文件。
# 函数用途: 将原端口丢失传给请求出口，避免当普通失败释放P3。
def missing_request_context_rejection(phase):
    from .multiprocess_background_execution import _uncertain_execution, current_background_request
    from .multiprocess_coordinator_execution import CoordinatorExecution
    from .request_binding import GatewayActiveTurnTransition

    scope = current_background_request()
    if scope is not None:
        return _uncertain_execution(scope.transition, phase)
    execution = current_worker_execution()
    if type(execution) in (WorkerExecution, CoordinatorExecution):
        claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
        transition = GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id)
        return _uncertain_execution(transition, phase)
    return RequestExecutionContextUnavailable("original_request_transaction_ports_required")


# LLM: actual invocation传入同一个Agent，BG/child只嵌套同源回调；Count0不构造source、不读文件，scope关闭使所有捕获端口失效。
# 函数用途: 为真实请求同步区间安装公共写端口，并恢复原外层上下文。
@contextmanager
def request_transaction_ports_scope(agent, *, background=None):
    from ..common.executor_registration import managed_request_execution_required
    from .multiprocess_coordinator_execution import CoordinatorExecution

    execution = current_worker_execution()
    if execution is None and not managed_request_execution_required():
        with request_execution_ports_scope(None):
            yield
        return
    if type(execution) not in (WorkerExecution, CoordinatorExecution) or execution.agent is not agent:
        raise missing_request_context_rejection("transaction_scope")
    source = OriginalRequestWriteSource(execution, background)
    try:
        with request_execution_ports_scope(source):
            yield
    finally:
        source._active = False
