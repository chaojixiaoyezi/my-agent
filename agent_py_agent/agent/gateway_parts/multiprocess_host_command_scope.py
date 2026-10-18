# LLM: 本地主体宿主命令沿原RuntimeDB/ToolExecutor claim和原C短写门；不伪造ask/P3或W，不建立第二操作账。
# 模块用途: 将真实管理员冷配置短提交绑定到原持锁C、原宿主命令和已认证身份。
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from ..auth.models import Permission, Role
from ..common.global_write_guard import require_global_writer
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    request_execution_ports_scope,
)
from ..local_storage.tool_operations import TOOL_OPERATION_RUNNING, ToolOperationRecord
from ..runtime_db.host_commands import HostCommandBinding
from ..runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ManagedOperationStore,
    ToolOperationAuthorityRequest,
)
from ..runtime_db.operations import RuntimeConflictError
from ..runtime_db.schema import runtime_db_path
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .io import gateway_turn_transition
from .multiprocess_control_forward import _coordinator_context_paths
from .multiprocess_coordinator import coordinator_for_root, coordinator_role_present
from .multiprocess_global_write import GlobalWriteError
from .multiprocess_route import owner_digest


# LLM: 捕获只来自已认证组合根；无C默认返回None且不读取库/线程，原C缺失不能退成默认模式。
# 函数用途: 为原宿主命令准备精确C短写来源，不启动进程或生成操作。
def capture_host_command_writer(base_agent, paths, repo, binding, scope_owner, *, permission_provider):
    require_global_writer("host_global_configuration")
    context = _coordinator_context_paths(base_agent, paths)
    lease = coordinator_for_root(paths.root, context=context)
    if lease is None:
        if coordinator_role_present(paths.root, context=context):
            raise GlobalWriteError("coordinator_context_unavailable", "context")
        return None
    if type(binding) is not HostCommandBinding or scope_owner != OwnerIdentity.local_main():
        raise GlobalWriteError("principal_mismatch", "owner")
    return CoordinatorHostCommandWriter(base_agent, paths, repo, binding, scope_owner,
                                        permission_provider, context, lease)


# LLM: source只保存原引用/claim，不是运行权；copy、跨PID/线程、失原C或已结操作都不能重新执行。同步原HCM及C生命周期测试。
# 类用途: 保存同一已认证宿主命令的原C和实际工具领取，在真正短写前重新核对。
class CoordinatorHostCommandWriter:
    # LLM: 构造无写入；canonical owner/db/thread来源由宿主确定，permission callback不得来自报文或角色字段。
    # 函数用途: 捕获本次原C、本地主体和宿主命令，尚不允许任何实际写入。
    def __init__(self, agent, paths, repo, binding, owner, permission_provider, context, lease):
        self.agent, self.paths, self.repo, self.binding = agent, paths, repo, binding
        self.owner, self.permission_provider, self.context, self.lease = owner, permission_provider, context, lease
        self.controller, self.pid, self.thread_id = lease.controller, os.getpid(), threading.get_ident()
        self.home = resolve_owner_home(agent.home_paths.root, owner)
        self.database_path = runtime_db_path(self.home.home_dir)
        self._identity, self._claim, self._operation_entered = self, None, False
        self._resource_scopes = None
        self._check_location()

    # LLM: 只用于区别真正operation尚未进入与可能已有部分副作用，不提供重试许可或持久状态。
    # 函数用途: 告诉原回执链本次同步写是否已经开始。
    @property
    def operation_entered(self):
        return self._operation_entered

    # LLM: 唯一调用者是原HCM observer，原execute记录和同request资源tuple不能重算、重绑或从current补代次。
    # 函数用途: 在handler之前保存原工具领取及实际声明资源的不可变身份。
    def bind_original_claim(self, record, resource_scopes):
        self._check_creator()
        if (self._claim is not None or type(record) is not ToolOperationRecord
                or record.status != TOOL_OPERATION_RUNNING or record.holder_pid != self.pid):
            raise GlobalWriteError("original_claim_required", "operation")
        if (type(resource_scopes) is not tuple or len(set(resource_scopes)) != len(resource_scopes)
                or any(type(scope) is not str or not scope for scope in resource_scopes)):
            raise GlobalWriteError("original_claim_required", "resources")
        self._check_claim_identity(record)
        self._claim, self._resource_scopes = record, resource_scopes

    # LLM: T→原controller/P2→原lease短门→原资源同序；operation必须是同步原文件写，不能等待模型/网络/审批/join。
    # 函数用途: 最后复核实际C、身份、线程、原claim后仅进入一次短提交。
    def perform(self, operation):
        self._check_creator()
        scope = _HOST_COMMAND_SOURCE.get()
        if type(scope) is not OriginalHostCommandWriteSource or scope.writer is not self:
            raise GlobalWriteError("original_source_required", "context")
        scope.require_original("host_global_configuration")
        require_global_writer("host_global_configuration")
        if self._operation_entered or self._claim is None or self.controller is None:
            raise GlobalWriteError("original_claim_required", "operation")
        results = []

        # LLM: 原launch_owner_writer同门调用，最终权限/claim回读先于副作用；进入后异常不能说零副作用。
        # 函数用途: 在本次C短门内执行已核对的原配置写。
        def commit():
            self._check_current()
            self._operation_entered = True
            results.append(operation())

        with gateway_turn_transition(self.paths, self.binding.request.operation_id):
            decision = self.controller.launch_owner_writer(owner_digest(self.owner), commit)
        if decision.get("status") != "started" or len(results) != 1:
            raise GlobalWriteError("coordinator_context_unavailable", "context")
        return results[0]

    # LLM: 当前身份只做限制，复制对象或fork/copy_context跨线程不能消费原claim；不从任意字符串恢复角色。
    # 函数用途: 拒绝脱离原同步宿主调用的source。
    def _check_creator(self):
        if (self._identity is not self or self.pid != os.getpid()
                or self.thread_id != threading.get_ident()):
            raise GlobalWriteError("original_source_required", "context")

    # LLM: 库和owner根必须仍是原本地主体规范地址，不读取客户端路径；路径通过不等于当前claim授权。
    # 函数用途: 检查原宿主运行库与C基础home的归属。
    def _check_location(self):
        home = self.agent.home_paths
        if (self.repo.db_path != self.database_path or self.binding.request.owner_id != self.home.owner_id
                or (home.owner_provider, home.owner_kind, home.owner_id) != ("local", "main", "local/main")
                or home.owner_home_dir != self.home.home_dir):
            raise GlobalWriteError("original_source_mismatch", "owner")

    # LLM: 原claim的输入/身份来自唯一ToolExecutor，不能只核operation_id或用fresh generation覆盖原holder。
    # 函数用途: 核对实际领取与原HostCommand绑定完全对应。
    def _check_claim_identity(self, record):
        binding, request = self.binding, self.binding.request
        if ((record.owner_id, record.run_id, record.task_id, record.operation_id, record.tool, record.args_hash)
                != (request.owner_id, binding.run_id, binding.task_id, request.operation_id,
                    request.command_name, "sha256:" + request.input_digest)):
            raise GlobalWriteError("original_call_mismatch", "operation")

    # LLM: 在原C生命周期短门实读权限/线程/原HCM及Store，未知/变代/结算均拒，不以pin、lease年龄或自然语言放行。
    # 函数用途: 在实际文件副作用前最后核所有原事实。
    def _check_current(self):
        store, authority = self._check_binding_current()
        request = self.binding.request
        if self._resource_scopes:
            store.require_authority(authority, claim=self._claim, resource_scopes=self._resource_scopes)
        else:
            store.require_authority(authority)
        current = store.get_tool_operation(owner_id=request.owner_id, run_id=self.binding.run_id,
            task_id=self.binding.task_id, attempt_id=self.binding.attempt_id,
            operation_id=request.operation_id, tool_name=request.command_name,
            args_hash="sha256:" + request.input_digest)
        keys = ("holder_id", "holder_host", "holder_pid", "holder_process_start_token", "generation", "status")
        if current is None or any(getattr(current, key) != getattr(self._claim, key) for key in keys):
            raise GlobalWriteError("original_claim_changed", "operation")

    # LLM: metadata核原binding/current attempt归属及C/ACL/thread，允许同活区间刚收口后的退出元数据；业务perform仍另核原Store running/claim/resources。
    # 函数用途: 核宿主元数据仍属于本次原运行，不能从fresh attempt或新许可补收口。
    def _check_binding_current(self):
        self._check_creator()
        self._check_location()
        if (coordinator_for_root(self.paths.root, context=self.context) is not self.lease
                or self.lease.controller is not self.controller):
            raise GlobalWriteError("coordinator_context_unavailable", "context")
        self._check_permission_thread()
        original = self.repo.find_host_command(self.binding.request)
        fields = ("request", "task_id", "task_run_id", "agent_run_id", "run_id", "attempt_id")
        if original is None or any(getattr(original, field) != getattr(self.binding, field) for field in fields):
            raise GlobalWriteError("original_call_mismatch", "operation")
        store, request = ManagedOperationStore(self.repo), self.binding.request
        authority = ToolOperationAuthorityRequest(request.owner_id, self.binding.run_id,
            self.binding.task_id, request.operation_id, request.command_name, self.binding.attempt_id)
        run = self.repo.get_agent_run(self.binding.agent_run_id)
        attempt = self.repo.get_attempt(self.binding.attempt_id)
        if (run is None or attempt is None or run["run_id"] != self.binding.run_id
                or run["current_attempt_id"] != self.binding.attempt_id
                or attempt["agent_run_id"] != self.binding.agent_run_id):
            raise GlobalWriteError("original_call_mismatch", "operation")
        return store, authority

    # LLM: 每次同原认证provider重判，Permission必须匹配原actor/channel；线程来源属于原local/main且通道绑定未变。
    # 函数用途: 复核原管理员及同一会话，不能靠正文admin或其它用户线程提权。
    def _check_permission_thread(self):
        request, permission = self.binding.request, self.permission_provider()
        if (type(permission) is not Permission or permission.role is not Role.ADMIN
                or permission.user_id != request.actor_id or request.channel not in permission.channels):
            raise GlobalWriteError("principal_mismatch", "permission")
        thread, error = self.agent.conversation_store.threads.load_report(request.thread_id)
        if (error is not None or thread is None or thread.owner_id != self.home.owner_id
                or thread.owner_home != str(self.home.home_dir) or thread.canonical_user_id != request.actor_id
                or not any(item.channel == request.channel and item.channel_user_id == request.actor_id
                           and item.thread_id == request.thread_id for item in thread.channel_bindings)):
            raise GlobalWriteError("principal_mismatch", "thread")


_HOST_COMMAND_SOURCE = ContextVar("original_host_command_transaction_source", default=None)


# LLM: 只运输原HostCommand写者的callback门；原current pointer/共享active/creator/pid/thread共同核，不能生成P3/P5或把C角色当许可。
# 类用途: 为一次实际宿主命令的元数据保存短提交来源。
class OriginalHostCommandWriteSource:
    # LLM: writer必须是组合根捕获的原对象；此处纯内存，不注册命令、不创建claim或读取库。
    # 函数用途: 保存这次原C宿主命令的精确写者引用。
    def __init__(self, writer):
        if type(writer) is not CoordinatorHostCommandWriter:
            raise RequestExecutionContextUnavailable("original_host_command_writer_required")
        self.writer = self._writer_origin = writer
        self._binding_origin = writer.binding
        self._origin, self._pid, self._thread, self._active = self, os.getpid(), threading.get_ident(), True

    # LLM: 原scope退出共同失效，复制、换writer、跨线程/PID及丢current pointer均拒；许可仍在callback实际门内核。
    # 函数用途: 拒绝不是当前原安装区间的宿主元数据源。
    def require_original(self, phase):
        if (type(self) is not OriginalHostCommandWriteSource or self._origin is not self
                or not self._active or self._pid != os.getpid() or self._thread != threading.get_ident()
                or self.writer is not self._writer_origin or self.writer.binding is not self._binding_origin
                or _HOST_COMMAND_SOURCE.get() is not self):
            raise RequestExecutionContextUnavailable("original_host_command_scope_required")
        try:
            self.writer._check_creator()
        except GlobalWriteError as error:
            raise self.rejection(phase) from error

    # LLM: HCM没有Gateway claim，拒绝不能捏造ASK欠账；仅报告原宿主区间缺失，不给重跑或任何新权限。
    # 函数用途: 为复制或退出的公共cell提供准确typed拒绝。
    def rejection(self, phase):
        return RequestExecutionContextUnavailable("original_host_command_scope_required")

    # LLM: 完整leaf只在原launch callback内；validation拒绝转common typed缺来源，业务callback异常原样透传，不凭phase授信。
    # 函数用途: 在原T/C/P2/ACL短窗提交同HCM元数据，来源丢失时保留原责任。
    def short_operation(self, phase, operation):
        self.require_original(phase)
        writer, results = self.writer, []

        # LLM: callback执行期间原controller与lease门仍持有；在操作前重新核原Host binding/ACL/thread，不补freshclaim。
        # 函数用途: 只在原C写者实际调用区间运行这段元数据leaf。
        def commit():
            self.require_original(phase)
            try:
                writer._check_binding_current()
            except (GlobalWriteError, RuntimeConflictError, AuthorityContextMissing) as error:
                raise self.rejection(phase) from error
            results.append(operation())

        with gateway_turn_transition(writer.paths, writer.binding.request.operation_id):
            decision = writer.controller.launch_owner_writer(owner_digest(writer.owner), commit)
        if decision.get("status") != "started" or len(results) != 1:
            raise self.rejection(phase)
        return results[0]

    # LLM: 本源只有callback区间，禁止通用with把Host许可延长到launch返回之后；ASK源仍支持原contextmanager。
    # 函数用途: 明确拒绝在宿主命令中使用未实现的yield式短门。
    def short_scope(self, phase):
        raise RequestExecutionContextUnavailable("host_command_callback_required")

    # LLM: 无可yield的原持T窗口就没有inner writer豁免，不借Host角色补许可。
    # 函数用途: 拒绝脱离原callback的内部元数据写者。
    def require_writer(self, phase):
        raise RequestExecutionContextUnavailable("host_command_callback_required")


# LLM: 仅可信HCM composition在其原binding已产生后安装，default路径不调用；finally使所有捕获cell共同失效，nested按token恢复。
# 函数用途: 在实际宿主命令期间运输一次原C元数据提交源。
@contextmanager
def host_command_transaction_scope(writer):
    source = OriginalHostCommandWriteSource(writer)
    token = _HOST_COMMAND_SOURCE.set(source)
    try:
        with request_execution_ports_scope(source):
            yield
    finally:
        source._active = False
        _HOST_COMMAND_SOURCE.reset(token)
