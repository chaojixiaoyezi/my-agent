# LLM: 只消费原CoordinatorExecution和唯一ToolExecutor实际领取；原T/P3/P5/C/P2/lease末次核后才同步写，不造操作账或W身份。
# 模块用途: 把本地主体工具的全局叶子接到已有短提交门，长模型/审批/handler仍在门外。
import os
import threading
import time

from ..local_storage.tool_operations import (
    TOOL_OPERATION_RUNNING,
    ToolOperationClaim,
    ToolOperationRecord,
)
from ..runtime_db.execution_mode import ExecutionMode
from ..runtime_db.managed_operation_store import (
    ManagedOperationStore,
    ToolOperationAuthorityRequest,
)
from ..tooling.tool_operation_coordinator import ToolOperationExecutionRequest
from .multiprocess_coordinator_execution import (
    CoordinatorExecution,
    current_coordinator_execution,
    require_coordinator_execution_agent,
)
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_global_write import GlobalWriteError
from .request_binding import GatewayActiveTurnTransition


# LLM: 组合根只为原正C创建；构造纯内存，无源核I/O/claim/P1/线程。Count0不创建，源权限仍须perform每次重核。
# 类用途: 保存当前原C引用，在实际叶子线程创建原工具短写来源。
class CoordinatorToolWriteFactory:
    # LLM: actual execution来自原scope，不能从字段或JSON拼装；同实例/PID印记防copy/fork。
    # 函数用途: 捕获本轮原协调者调用，尚不允许任何副作用。
    def __init__(self, execution):
        self.execution, self.pid, self.origin = execution, os.getpid(), self

    # LLM: common共享cell guard决定运输寿命，actualC scope必须同对象；构造source不授新claim。
    # 函数用途: 在真实工具最后叶子为原领取建立短门提交器。
    def capture(self, request, claim, guard):
        if (self.origin is not self or self.pid != os.getpid() or type(self.execution) is not CoordinatorExecution
                or current_coordinator_execution() is not self.execution):
            raise GlobalWriteError('original_source_required', 'context')
        guard()
        return CoordinatorToolWriteSource(self.execution, request, claim, guard).perform


# LLM: 保存原C/Store/request/claim及实际资源tuple，不能用fresh current holder/generation补旧权；managed/unmanaged源各守原事实。
# 类用途: 在同步全局写前重核本轮原C和实际工具领取。
class CoordinatorToolWriteSource:
    # LLM: 构造只保存原引用和创建线程；Gateway回调捕获仍安装的原C，不新建请求、claim或状态。
    # 函数用途: 固定实际工具的原运行权与叶子执行者。
    def __init__(self, execution, request, claim, guard):
        if (type(request) is not ToolOperationExecutionRequest or type(claim) is not ToolOperationClaim
                or claim.action != 'execute' or type(claim.record) is not ToolOperationRecord):
            raise GlobalWriteError('original_claim_required', 'operation')
        self.execution, self.request, self.claim, self.guard = execution, request, claim, guard
        self.gate, self.agent = execution.gate, execution.agent
        self.controller = getattr(self.gate, '_runtime_takeover', None)
        if self.controller is None:
            raise GlobalWriteError('original_source_required', 'context')
        self.lease = self.controller._lease
        self.mode = self.agent.subagents.execution_mode
        self.pid, self.thread, self.origin = os.getpid(), threading.get_ident(), self
        self.transition = GatewayActiveTurnTransition(execution.claim.path, execution.claim.request_id,
                                                     execution.claim.execution_attempt_id)
        self._check_original()

    # LLM: T→原controller/P2→原lease→叶子资源锁；该短callback不得网络/审批/等待，嵌套由common活窗口复用。
    # 函数用途: 最后重核原运行权与claim后仅执行当前同步写。
    def perform(self, operation):
        self._check_original()

        # LLM: 原GatewayActiveTurnTransition已经持T/C门，争门后仍核同原引用和实际账；原源变化一律失败。
        # 函数用途: 在真实原短门内执行本次全局叶子。
        def commit():
            self._check_original()
            self._check_claim()
            return operation()

        return self.transition('global_tool_commit', commit)

    # LLM: 原main或自产child绑定须同actualAgent；controller/Store/工具claim仍原对象，guard不代替T/P3/P5，不修改gate.agent。
    # 函数用途: 拒绝复制、跨执行者、source丢失或权威库重绑。
    def _check_original(self):
        self.guard()
        try:
            require_coordinator_execution_agent(self.execution)
        except WorkerDispatchError as error:
            raise GlobalWriteError('original_source_changed', 'context') from error
        if (self.origin is not self or self.pid != os.getpid() or self.thread != threading.get_ident()
                or current_coordinator_execution() is not self.execution or self.execution.creator_pid != self.pid
                or self.execution.gate is not self.gate or self.execution.agent is not self.agent
                or self.gate._runtime_takeover is not self.controller
                or self.controller._lease is not self.lease or self.lease.controller is not self.controller
                or self.transition._worker_execution is not self.execution
                or self.request.store is not self.agent._operation_store
                or self.request.store is not self.agent.tools.operation_store
                or self.agent.subagents.execution_mode is not self.mode):
            raise GlobalWriteError('original_source_changed', 'context')
        if self.mode is ExecutionMode.MANAGED:
            if (type(self.request.store) is not ManagedOperationStore
                    or self.request.store._repo is not self.agent.subagents.runtime_db):
                raise GlobalWriteError('original_source_changed', 'store')
        elif self.mode is not ExecutionMode.LOCAL_UNMANAGED or self.request.store is not self.agent.local_store:
            raise GlobalWriteError('original_source_changed', 'store')

    # LLM: 所有原身份/输入/holder/gen/status及有效租约末次核；managed核真实SQLite资源，local不伪造该资源账。
    # 函数用途: 回读同一实际工具领取，拒绝终态、换代、输入漂移及丢锁。
    def _check_claim(self):
        request, original = self.request, self.claim.record
        if (original.status != TOOL_OPERATION_RUNNING or original.holder_pid != self.pid
                or original.generation < 1 or type(request.resource_scopes) is not tuple
                or request.owner_id != self.agent.home_paths.owner_id
                or self.agent.home_paths.owner_id != 'local/main'
                or len(set(request.resource_scopes)) != len(request.resource_scopes)
                or any(type(scope) is not str or not scope for scope in request.resource_scopes)
                or (original.owner_id, original.run_id, original.task_id, original.operation_id,
                    original.tool, original.args_hash) != (request.owner_id, request.run_id, request.task_id,
                    request.operation_id, request.tool_name, request.args_hash)):
            raise GlobalWriteError('original_claim_required', 'operation')
        if self.mode is ExecutionMode.MANAGED:
            authority = ToolOperationAuthorityRequest(request.owner_id, request.run_id, request.task_id,
                request.operation_id, request.tool_name, request.attempt_id)
            if request.resource_scopes:
                request.store.require_authority(authority, claim=original, resource_scopes=request.resource_scopes)
            else:
                request.store.require_authority(authority)
            current = request.store.get_tool_operation(owner_id=request.owner_id, run_id=request.run_id,
                operation_id=request.operation_id, task_id=request.task_id, attempt_id=request.attempt_id,
                tool_name=request.tool_name, args_hash=request.args_hash, resource_scopes=request.resource_scopes)
        else:
            current = request.store.get_tool_operation(owner_id=request.owner_id, run_id=request.run_id,
                                                       operation_id=request.operation_id)
        keys = ('owner_id', 'run_id', 'task_id', 'operation_id', 'tool', 'args_hash', 'holder_id',
                'holder_host', 'holder_pid', 'holder_process_start_token', 'generation', 'status')
        if (current is None or any(getattr(current, key) != getattr(original, key) for key in keys)
                or current.lease_expires_at <= time.time()):
            raise GlobalWriteError('original_claim_changed', 'operation')
