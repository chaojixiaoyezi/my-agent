# LLM: 只有原CoordinatorExecution安装本地主体工具登记，零模式不创建source/账本/域；C-host只用原C native强source，拒legacy丢源delegate；同步C/P1/host合同。
# 模块用途: 将本地已认领请求的工具调用送入原 P1 两阶段启动，Linux 命令使用同一原 C 创建的内核域。
import os
import uuid
from contextlib import contextmanager

from ..common.executor_registration import (
    ExecutorRegistrationError,
    current_executor_tool_call,
    executor_scope,
)
from .multiprocess_coordinator_execution import CoordinatorExecution
from .multiprocess_executor_runtime import _command, _reservation
from .multiprocess_executor_start import start_executor
from .multiprocess_route import owner_digest


# LLM: 安装时只保存原 C 内存对象，真正工具调用才核生产 controller 并建立 source；组件只捕获回调不增加 I/O。
# 类用途: 在当前 C 请求内延迟创建原执行登记来源，规范 tool_call_id 在实际工具边界读取。
class _CoordinatorExecutorLauncher:
    # LLM: 上下文只由原组合根传入，创建 PID 拒绝 fork 继承，不从请求字段恢复权限。
    # 函数用途: 捕获本次真实 C 执行，尚不执行任何磁盘或进程操作。
    def __init__(self, execution):
        self.execution, self.creator_pid = execution, os.getpid()
        self._execution_origin = execution
        self._origin, self._source = self, None

    # LLM: 原T/P3/P5/C门决定权限；新预留不fallback/重放；普通Linux命令进原内核域，后台host仅原C native强source设施v1，不能legacy裸ledger。
    # 函数用途: 为一个已批准工具调用完成先登记后放行，返回原真实 Popen 等待链路。
    def launch(self, kind, argv, options):
        from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
        from .multiprocess_native_linux_domain import prepare_native_domain

        if self._origin is not self or self.creator_pid != os.getpid() or self.execution is not self._execution_origin:
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_CONTEXT_REQUIRED', 'stage': 'binding'})
        call_id = current_executor_tool_call()
        if call_id is None:
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_TOOL_CALL_REQUIRED', 'stage': 'binding'})
        if options.delegate is not None and (
                options.delegate != 'background_host' or kind != 'background_host' or options.shell):
            raise ExecutorRegistrationError({'error_code': 'EXECUTOR_COORDINATOR_DELEGATE_UNAVAILABLE', 'stage': 'delegate'})
        source = self._source
        if source is None:
            source = self._source = CoordinatorExecutorSource(self.execution)
        source._require_source_identity()
        claim = self.execution.claim
        binding = {'owner_sha': owner_digest(source.owner), 'request_id': claim.request_id,
                   'execution_attempt_id': claim.execution_attempt_id, 'lease_epoch': claim.lease_epoch,
                   'tool_call_id': call_id}
        row = _reservation(source, binding, kind, 'exec-' + uuid.uuid4().hex)
        command = _command(argv, options)
        try:
            if options.delegate is not None:
                from .multiprocess_native_coordinator_host import launch_coordinator_native_host

                result = launch_coordinator_native_host(source, row, command)
            else:
                command, row = prepare_native_domain(command, row, source, source._domains)
                if command.native_domain is not None:
                    command.native_domain.retain_lifetime()
                result = start_executor(source, row, command)
        finally:
            source.collect_completed_domains()
        if result is None or not result.ok:
            raise ExecutorRegistrationError(result.error if result is not None else {'error_code': 'EXECUTOR_CONTEXT_REQUIRED'})
        return result.process


# LLM: 只在原 C scope 已设置后安装；gate=None/count=0 保持原 ContextVar 与无 I/O 路径，不改变 W 进程级拒绝门。
# 函数用途: 为本地原认领工具调用安装登记门，退出精确恢复上一层作用域。
@contextmanager
def coordinator_request_executor_scope(execution):
    if execution is None:
        yield
        return
    if type(execution) is not CoordinatorExecution or execution.creator_pid != os.getpid():
        raise ExecutorRegistrationError({'error_code': 'EXECUTOR_CONTEXT_REQUIRED', 'stage': 'binding'})
    if execution.gate.count == 0:
        yield
        return
    launcher = _CoordinatorExecutorLauncher(execution)
    with executor_scope(launcher.launch):
        yield
