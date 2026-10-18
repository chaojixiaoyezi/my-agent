# LLM: 原BG claim定位投影须在同一短T内发布；只组合原P3/P5，窗口只认原lifetime当前同对象token，同步事务/claim/撤权测试。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 在原请求执行域内调用后台业务，并把精确Gateway运输身份接到原模型提交及RuntimeDB身份发布。
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..user_space.owner_resolver import _owner_home_dir
from .io import read_json_file_report, update_json_file_atomic
from .multiprocess_background_request import (
    BackgroundSourceChanged,
    read_source,
    source_owner,
    validate_source_ref,
)
from .multiprocess_coordinator_execution import CoordinatorExecution
from .multiprocess_execution import WorkerExecution, current_worker_execution
from .request_binding import GatewayActiveTurnTransition

_BACKGROUND_REQUEST = ContextVar("gateway_background_request", default=None)
_BACKGROUND_TRANSACTION = ContextVar("gateway_background_transaction", default=None)


# LLM: 原scope共享对象只描述活性及当前真实T窗口；origin/self拒复制，窗口只能在原T内发布/清空，不独立授P5许可。
# 类用途: 保留原后台有效期及当前短事务的准确对象，退出/fork不能延寿。
@dataclass
class BackgroundScopeLifetime:
    creator_pid: int
    active: bool = True
    _origin: object = field(init=False, repr=False)
    _active_window: BackgroundTransaction | None = field(default=None, init=False, repr=False)

    # LLM: origin指回原构造对象，浅复制不能成为新原scope；纯内存限制，无I/O或准入。
    # 函数用途: 给原共享生命周期记录自身身份。
    def __post_init__(self):
        self._origin = self


# LLM: 仅实际原T窗口发布；exact type/origin/self及原lifetime当前pointer同对象才可nested复用，副本/重造/退出必须拒。
# 类用途: 描述真正持有原非重入T的准确短事务，active布尔值本身不代表持锁。
@dataclass
class BackgroundTransaction:
    transition: GatewayActiveTurnTransition
    creator_pid: int
    thread_id: int
    active: bool = True
    _origin: object = field(init=False, repr=False)

    # LLM: 原窗口身份不从字段相等推导，复制对象保旧origin因此不能冒充原持锁窗口。
    # 函数用途: 保存本次真实短事务的自身对象身份。
    def __post_init__(self):
        self._origin = self


# LLM: scope只捕获原claimed请求/Agent/transition；不能从保存JSON重建执行上下文或作为独立后台状态。
# 类用途: 保留本次后台业务调用的原Gateway执行边界。
@dataclass(frozen=True)
class BackgroundRequestExecution:
    context: dict
    transition: GatewayActiveTurnTransition
    execution: object
    lifetime: BackgroundScopeLifetime


# LLM: 原P5已经建立worker_execution_scope；此处不得初始化Agent、改owner或重新领取来源，只安装临时组合上下文。
# 函数用途: 将准确运输回合贯穿后台同步调用，并在异常后恢复外层上下文。
@contextmanager
def background_request_scope(context):
    execution = current_worker_execution()
    if type(execution) not in (WorkerExecution, CoordinatorExecution) or execution.agent is not context["agent"]:
        raise ValueError("background_original_worker_execution_required")
    request = context["request"]
    transition = GatewayActiveTurnTransition(context["request_path"], context["request_id"], request["execution_attempt_id"])
    lifetime = BackgroundScopeLifetime(os.getpid())
    token = _BACKGROUND_REQUEST.set(BackgroundRequestExecution(context, transition, execution, lifetime))
    try:
        from .request_transactions import request_transaction_ports_scope

        with request_transaction_ports_scope(context["agent"], background=_BACKGROUND_REQUEST.get()):
            background_short_operation("background_source_admission", lambda: None)
            yield
    finally:
        lifetime.active = False
        _BACKGROUND_REQUEST.reset(token)


# LLM: 返回原捕获transition而非许可；长线程只能在每个短操作重新核同claim，不能据此借terminal后身份新执行。
# 函数用途: 为原租约心跳显式捕获当前运输边界。
def background_transition():
    current = _BACKGROUND_REQUEST.get()
    return current.transition if current is not None else None


# LLM: 只运输当前原claimed对象，不能从JSON或role重建；新child组合须保原shared lifetime并独立核其canonical谱系。
# 函数用途: 让同步原子代理工作片捕获自己这次运输的原事实引用。
def current_background_request():
    return _BACKGROUND_REQUEST.get()


# LLM: child真实Agent已沿原P5 home和canonical run核对；共享outer lifetime在运输终态后拒late线程，非重新签发许可。
# 函数用途: 为同一次已认领child运输安装原实际worker的短提交上下文。
@contextmanager
def background_child_scope(context, execution, lifetime):
    transition = GatewayActiveTurnTransition(context["request_path"], context["request_id"], context["request"]["execution_attempt_id"])
    token = _BACKGROUND_REQUEST.set(BackgroundRequestExecution(context, transition, execution, lifetime))
    try:
        from .request_transactions import request_transaction_ports_scope

        with request_transaction_ports_scope(context["agent"], background=_BACKGROUND_REQUEST.get()):
            background_short_operation("child_source_admission", lambda: None)
            yield
    finally:
        _BACKGROUND_REQUEST.reset(token)


# LLM: T先取、业务锁后取，operation未进入的权限失败才变typed未结事实；业务自身异常原样返回，Count0无scope直调。
# 函数用途: 在原Gateway执行权仍可核实的短门里完成一次真实业务事务。
def background_short_operation(phase, operation, *, transition=None):
    with background_short_scope(phase, transition=transition):
        return operation()


# LLM: 原T/C commit context覆盖短body；nested只复用原lifetime当前exact同源token，finally失效并清pointer，门未入才typedunknown。
# 函数用途: 为显式文件事务保留当次原回合权限，body异常保持实际副作用语义。
@contextmanager
def background_short_scope(phase, *, transition=None):
    current = _BACKGROUND_REQUEST.get()
    selected = transition or (current.transition if current is not None else None)
    if selected is None:
        yield
        return
    if current is None or selected is not current.transition:
        raise _uncertain_execution(selected, phase)
    held = _BACKGROUND_TRANSACTION.get()
    if not _original_background_lifetime_active(current.lifetime):
        raise _uncertain_execution(selected, phase)
    if held is not None:
        if not _holds_original_background_window(current, held):
            raise _uncertain_execution(selected, phase)
        yield
        return
    entered = False
    transaction = BackgroundTransaction(selected, os.getpid(), threading.get_ident())

    try:
        with selected.scope(phase):
            if not _original_background_lifetime_active(current.lifetime):
                raise _uncertain_execution(selected, phase)
            entered = True
            current.lifetime._active_window = transaction
            token = _BACKGROUND_TRANSACTION.set(transaction)
            try:
                yield
            finally:
                transaction.active = False
                current.lifetime._active_window = None
                _BACKGROUND_TRANSACTION.reset(token)
    except Exception as error:
        if entered:
            raise
        raise _uncertain_execution(selected, phase) from error


# LLM: 原inner写者不能倒取T；必须核原lifetime当前same-object窗口及exact type/origin/PID/thread，副本active不能绕末写，Count0直返。
# 函数用途: 在原内部写者落盘前确认外层短事务仍真正持有T。
def require_background_transaction(phase):
    current = _BACKGROUND_REQUEST.get()
    if current is None:
        return
    held = _BACKGROUND_TRANSACTION.get()
    if not _holds_original_background_window(current, held):
        raise _uncertain_execution(current.transition, phase)


# LLM: 只核原共享生命周期的exact type/origin/self/PID/活性，拒浅复制与子类；无I/O、不构造新的权限事实。
# 函数用途: 检查原后台scope仍在本进程有效。
def _original_background_lifetime_active(lifetime):
    return (type(lifetime) is BackgroundScopeLifetime and lifetime._origin is lifetime
            and lifetime.active is True and lifetime.creator_pid == os.getpid())


# LLM: 仅原lifetime当前发布的同对象窗口可复用；type/origin/pointer共同拒copy/子类/重造，原线程/PID/source检查仍保留。
# 函数用途: 判定当前嵌套写者是否仍真正处在原T短事务内。
def _holds_original_background_window(current, held):
    return (_original_background_lifetime_active(current.lifetime) and type(held) is BackgroundTransaction
            and held._origin is held and current.lifetime._active_window is held and held.active is True
            and held.transition is current.transition and held.creator_pid == os.getpid()
            and held.thread_id == threading.get_ident())


# LLM: 原claim不可变身份是唯一producer来源；不从当前漂移文件、普通异常文本或role bool重建执行权。
# 函数用途: 将准确原执行的未结事实交给已有请求异常出口。
def _uncertain_execution(transition, phase):
    from .request_errors import RequestExecutionOutcomeUncertainError

    execution = transition._worker_execution
    claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
    identity = {"id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner}
    return RequestExecutionOutcomeUncertainError(identity, phase)


# LLM: 无桥上下文的原single/后台调用直接返回，无I/O；有桥时只检查原T/P5，绝不持T等待供应商。
# 函数用途: 在后台供应商调用实际发出前复核原运输回合仍开放。
def require_background_model_admission():
    background_short_operation("submit", lambda: None)


# LLM: 原request authority后由同一活T发布原claim定位投影；投影不授权，不把后台invocation当Gateway运输ID。
# 类用途: 组合原后台确认回调和原Gateway运行身份投影，不发明模型task/run/attempt。
@dataclass(frozen=True)
class BackgroundRuntimeBinding:
    scope: BackgroundRequestExecution
    invocation_id: str
    previous: object

    # LLM: 原task-link确认仍由既有callback决定；桥不据task路径改归属、不代替原晋升事务。
    # 函数用途: 保留原后台任务链接确认。
    def __call__(self, link):
        if not self.scope.lifetime.active or self.scope.lifetime.creator_pid != os.getpid():
            raise _uncertain_execution(self.scope.transition, "bind_task")
        return background_short_operation("bind_task", lambda: True if self.previous is None else callable(self.previous) and self.previous(link) is True,
                                          transition=self.scope.transition)

    # LLM: 实际身份必须属于原后台invocation；原callback先确认，再在精确Gateway T/P5内写原字段，停止后不接受迟到发布。
    # 函数用途: 让原控制入口看到后台模型实际run/attempt，保持运输attempt独立。
    def bind_runtime_authority(self, binding):
        keys = ("task_id", "run_id", "agent_run_id", "attempt_id")
        if type(binding) is not dict or binding.get("invocation_run_id") != self.invocation_id or any(type(binding.get(key)) is not str or not binding[key] for key in keys):
            return False
        previous = getattr(self.previous, "bind_runtime_authority", None)
        context = self.scope.context
        authority = {"schema_version": "gateway_runtime_authority.v1", "request_id": context["request_id"],
                     "gateway_execution_attempt_id": context["request"]["execution_attempt_id"],
                     **{key: binding[key] for key in keys}}

        # LLM: 原T已持有；JSON更新保留并发租约/取消字段，只更新已发布身份的既有投影。
        # 函数用途: 原子保存后台实际模型归属，不改运输claim或原后台lease。
        def persist():
            if callable(previous) and previous(binding) is not True:
                return False
            update_json_file_atomic(context["request_path"], lambda current: {**current, "runtime_authority": authority}, require_existing=True)
            context["request"]["runtime_authority"] = authority
            return True

        if not self.scope.lifetime.active or self.scope.lifetime.creator_pid != os.getpid():
            raise _uncertain_execution(self.scope.transition, "bind_runtime_authority")
        return background_short_operation("bind_runtime_authority", persist, transition=self.scope.transition)

    # LLM: 仅具体原BG producer调用既有claim publisher，不接受外部写回调；同T复核原execution/context/lifetime及已发布authority。
    # 函数用途: 在原运输短门内把同一次core绑定与原会话claim一并写入定位投影。
    def publish_claim_runtime_authority(self, binding):
        from ..conversation.claim_execution_binding import publish_claim_runtime_authority

        return background_short_operation(
            "publish_claim_runtime_authority",
            lambda: publish_claim_runtime_authority(binding, gateway_binding=self),
            transition=self.scope.transition,
        )

    # LLM: 仅原publisher在已持T时读取；fence只取C.claim或W.binding.claim，实读原processing authority，缺失/中间态不补新身份。
    # 函数用途: 为原claim原子CAS生成精确运输定位字段，不签执行许可或写任何文件。
    def claim_transport_reference(self, binding):
        scope = self.scope
        execution, context = scope.execution, scope.context
        if (type(self) is not BackgroundRuntimeBinding or _BACKGROUND_REQUEST.get() is not scope
                or current_worker_execution() is not execution or execution.agent is not context["agent"]
                or scope.transition._worker_execution is not execution):
            raise _uncertain_execution(scope.transition, "publish_claim_runtime_authority")
        require_background_transaction("publish_claim_runtime_authority")
        claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
        fence = {"id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
                 "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner}
        keys = ("task_id", "run_id", "agent_run_id", "attempt_id")
        if (type(binding) is not dict or binding.get("invocation_run_id") != self.invocation_id
                or any(type(binding.get(key)) is not str or not binding[key] for key in keys)):
            raise _uncertain_execution(scope.transition, "publish_claim_runtime_authority")
        authority = {"schema_version": "gateway_runtime_authority.v1", "request_id": claim.request_id,
                     "gateway_execution_attempt_id": claim.execution_attempt_id,
                     **{key: binding.get(key) for key in keys}}
        report = read_json_file_report(claim.path, context="gateway.background_claim_transport.read")
        if (type(claim.lease_epoch) is not int
                or context["request_path"] != claim.path or context["request_id"] != claim.request_id
                or report.load_error is not None or report.payload.get("runtime_authority") != authority
                or context["request"].get("runtime_authority") != authority
                or any(type(record.get(key)) is not type(value) or record.get(key) != value
                       for record in (context["request"], report.payload) for key, value in fence.items())):
            raise _uncertain_execution(scope.transition, "publish_claim_runtime_authority")
        return {"schema": "gateway_claim_transport_reference.v1", "request_id": claim.request_id,
                "gateway_execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner,
                "invocation_run_id": self.invocation_id,
                **{"core_" + key: binding[key] for key in keys}}


# LLM: Gateway T为外层，后台原callback为内层，保持原T→业务锁序；只包reserve/submit/ack等短操作，不包模型等待。
# 类用途: 同时遵守运输回合和原后台回合的模型/插话提交边界。
@dataclass(frozen=True)
class BackgroundRequestTransition:
    gateway: GatewayActiveTurnTransition
    previous: object
    lifetime: BackgroundScopeLifetime

    # LLM: 原callback缺失时直接执行原operation；存在时原参数/返回值逐字保留，关闭门仍由Gateway拒绝。
    # 函数用途: 在两个既有短提交边界内同步执行一次真实操作。
    def __call__(self, phase, operation):
        if not self.lifetime.active or self.lifetime.creator_pid != os.getpid():
            raise _uncertain_execution(self.gateway, phase)
        return background_short_operation(phase, lambda: self.previous(phase, operation) if callable(self.previous) else operation(),
                                          transition=self.gateway)


# LLM: scope缺失必须返回同一个原参数对象；仅已认领桥调用组合原callback，保留原任务/调度run/invocation身份。
# LLM: 仅原当前主BG claim组装opaque input；orphan/task-local不继承邮箱，无scope Count0同对象。
# 函数用途: 给后台模型参数接入原运输、实际身份与主BG输入安全点。
def bind_background_run_params(agent, params):
    current = _BACKGROUND_REQUEST.get()
    if current is None:
        return params
    if current.execution.agent is not agent or current_worker_execution() is not current.execution:
        raise ValueError("background_original_worker_execution_required")
    ref = current.context["request"].get("metadata", {}).get("background_source", {})
    if ref.get("kind") == "orphan":
        task = agent.subagents.load(ref["source_id"])
        if (params.run_id != task.id or params.task_id != (task.root_id or task.id)
                or params.attempt_id != task.runner_active_attempt_id):
            raise ValueError("background_child_model_identity_changed")
    binding = BackgroundRuntimeBinding(current, params.request_id, params.conversation_task_binding_callback)
    from .multiprocess_background_input import bind_background_input
    pending = bind_background_input(agent, params, binding) if ref.get('kind') != 'orphan' else None
    return replace(params, pending_turn_input_source=pending,
        conversation_task_binding_callback=binding,
        active_turn_transition_callback=BackgroundRequestTransition(current.transition, params.active_turn_transition_callback, current.lifetime))


# LLM: 原请求kind来自已冻结P5；原source bytes/owner/store必须重核，来源漂移只结清运输，不消费新来源或构造模型prompt。
# 函数用途: 从原Gateway请求调用精确owner的原后台消费者，并返回结构化结果事实。
def execute_background_request(context):
    agent = context["agent"]
    ref = validate_source_ref(context["request"].get("metadata", {}).get("background_source"))
    owner = source_owner(ref["owner"])
    home = agent.home_paths
    if (home.owner_provider != owner.provider or home.owner_kind != owner.owner_kind
            or home.owner_home_dir != _owner_home_dir(home.root, owner)):
        raise ValueError("background_source_owner_mismatch")
    with background_request_scope(context):
        try:
            read_source(home.root, ref)
        except BackgroundSourceChanged:
            return {"source_kind": ref["kind"], "source_id": ref["source_id"], "outcome": "source_changed"}
        require_background_model_admission()
        if ref["kind"] in {"wake", "policy", "observation"}:
            return _execute_wake(agent, ref)
        if ref["kind"] == "scheduler":
            wake_ids = background_short_operation("scheduler_due_publication", agent.scheduler_service.enqueue_ready_runs)
            return {"source_kind": "scheduler", "source_id": ref["source_id"], "outcome": "handled", "wake_count": len(wake_ids)}
        if ref["kind"] == "learning":
            return _execute_learning(agent, ref)
        if ref["kind"] == "maintenance":
            return _execute_maintenance(agent, ref)
        if ref["kind"] == "harvester":
            from ..ingestion.harvest_execution import execute_harvester_source

            result = execute_harvester_source(agent, ref, read_source(home.root, ref))
            return {"source_kind": "harvester", "source_id": ref["source_id"], **result}
        if ref["kind"] == "orphan":
            consumer = getattr(agent, "_background_orphan_consumer", None)
            if not callable(consumer):
                raise ValueError("background_original_child_consumer_required")
            return consumer(ref)
        enabled = getattr(getattr(agent, "owner_policy", None), "memory_enabled", True)
        if not enabled:
            return {"source_kind": "curator", "source_id": ref["source_id"], "outcome": "handled", "status": "disabled"}
        result = agent.memory_curator.run_if_due()
        return {"source_kind": "curator", "source_id": ref["source_id"], "status": result.status,
                "run_id": result.run_id, "outcome": "pending" if result.status in {"busy", "not_due"} else "handled"}


# LLM: FIFO只能消费原精确路径；registry只运行原维护后返回，不消费后来FIFO/调模型/确认候选，memory开关不替代独立learning配置。
# 函数用途: 将已认领学习运输接到原无工具候选生成，并保忙/配额顺延在同一P3。
def _execute_learning(agent, ref):
    service = getattr(agent, "skill_learning", None)
    if service is None or not getattr(agent.config, "enable_self_learning", False):
        return {"source_kind": "learning", "source_id": ref["source_id"], "outcome": "handled", "status": "disabled"}
    if ref["source_ref"] == "data/skill_learning/registry.json":
        if service.settings.maintenance_mode != "autonomous":
            return {"source_kind": "learning", "source_id": ref["source_id"], "outcome": "handled", "status": "disabled"}
        result = service.run_pending(maintenance_only=True)
    else:
        result = service.run_pending(expected_request_path=agent.home_paths.owner_home_dir / ref["source_ref"])
    return {"source_kind": "learning", "source_id": ref["source_id"], "status": result.status,
            "outcome": "pending" if result.status in {"busy", "daily_limit", "retry"} else "handled"}


# LLM: 只消费实际owner原retention事实；原长扫描在T外，逐动作/审计/最终state重核当次claim，shared索引不授W写权。
# 函数用途: 让归属owner的原请求执行私有维护，并返回实际结构化结果。
def _execute_maintenance(agent, ref):
    from ..user_space.owner_maintenance import run_owner_retention_if_due

    result = run_owner_retention_if_due(agent.home_paths, include_global_indexes=False,
        commit_transition=lambda operation: background_short_operation("owner_maintenance_commit", operation),
        expected_policy_fingerprint=ref["input_revision"],
        expected_state_fingerprint=ref["source_sha256"] if ref["source_exists"] else "missing")
    return {"source_kind": "maintenance", "source_id": ref["source_id"], "outcome": "handled", **result.to_dict()}


# LLM: 原tick_thread保留wake→observation→policy顺序及持久claim/lease/handled合同；必须消费本Agent canonical会话store。
# 函数用途: 在准确运输执行域内推进原后台会话消费者，不复制唤醒或另建调度器状态账。
def _execute_wake(agent, ref):
    from ..conversation.runtime import BackgroundMainAgentRuntime, BackgroundMainAgentScheduler

    actual = Path(agent.conversation_store.storage.root).relative_to(agent.home_paths.owner_home_dir).as_posix()
    if actual != ref["store_ref"]:
        raise ValueError("background_source_store_mismatch")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=agent.delivery_service)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})
    reports = scheduler.tick_thread(ref["thread_id"])
    if ref["kind"] == "wake":
        pending = agent.conversation_store.wakes.pending_one(ref["source_id"])
    else:
        try:
            payload = read_source(agent.home_paths.root, ref)
        except BackgroundSourceChanged:
            pending = None
        else:
            from .multiprocess_background_discovery import _policy_has_due_work

            pending = (payload["observations"] or None) if ref["kind"] == "observation" else (
                payload if _policy_has_due_work(payload, time.time()) else None)
    return {"source_kind": ref["kind"], "source_id": ref["source_id"], "report_count": len(reports),
            "outcome": "handled" if pending is None else "pending"}


# LLM: 仅原消费者明确pending且原源字节仍相同时回同request；P3占用保留，实际重排由recovery原T/P5/lease CAS入口负责。
# 函数用途: 后台尚未处理的原来源让出模型槽，稍后由同一请求按原claim重新认领。
def requeue_pending_background_request(response, agent):
    from .paths import gateway_paths_from_root
    from .recovery import requeue_current_processing_request

    result = response.get("background_result")
    if type(result) is not dict or result.get("outcome") != "pending":
        return False
    execution = current_worker_execution()
    if type(execution) not in (WorkerExecution, CoordinatorExecution) or execution.agent is not agent:
        raise ValueError("background_original_worker_execution_required")
    claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
    from .io import read_json_file_report

    payload = read_json_file_report(claim.path, context="background.requeue.source").payload
    ref = validate_source_ref(payload.get("metadata", {}).get("background_source"))
    if result.get("source_kind") != ref["kind"] or result.get("source_id") != ref["source_id"]:
        raise ValueError("background_source_identity_changed")
    try:
        read_source(agent.home_paths.root, ref)
    except BackgroundSourceChanged:
        return False
    transition = GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id)
    paths = gateway_paths_from_root(claim.path.parent.parent.parent)
    return requeue_current_processing_request(paths, claim, transition, not_before_at=time.time() + 1.0)
