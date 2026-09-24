# LLM: 可选决策只有建议权；实验阶段另读原授权且当前联网失败关闭，普通增强保留原路径，期限及身份发送前后复核。
# 模块用途: 为原业务批次执行准确会话或用户后台范围的可选决策，不授予业务写入权。
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field

from ..backends.bounded_call import BoundedCallBusyError, BoundedCallTimeoutError
from ..backends.decision_protocol import (
    DecisionBinding,
    DecisionInputError,
    DecisionRequest,
    DecisionResponse,
)
from ..backends.errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderTimeoutError,
)
from ..backends.typesafe_decision import decision_backend_from_profile
from ..common.cancellation import ToolCancelled, raise_if_cancelled
from ..concurrency.interrupt import InterruptHandle, is_interrupted
from ..llm_scale.concurrency import ConcurrencyTimeout
from ..runtime_context import current_subagent_run_id, current_task_attributes
from ..settings.decision_settings import execute_decision_settings_operation
from ..settings.decision_settings_projection import decision_profile
from ..settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ..settings.model_profiles import model_profiles_path, read_model_profiles
from ..settings.model_provider_schema import ModelProfileError
from .decision_policy import (
    ActiveDecision,
    connection_revision,
    cooldown_state,
    decision_owner_ref,
    record_failure,
    record_success,
    register_active,
    unregister_active,
)


# LLM: 宿主准备前创建；experiment 只标记路径而非许可，当前无输入证明时 enabled_points 为空；scope/deadline 不跨身份复用。
# 类用途: 保存同一原业务批次共用的绝对预算与可信身份。
@dataclass(frozen=True)
class DecisionStage:
    operation_id: str
    owner_ref: str
    thread_id: str
    run_id: str
    task_id: str
    started_at: float
    deadline: float
    error_code: str = ""
    scope: str = "thread"
    enabled_points: tuple[str, ...] = ()
    experiment: bool = False


# LLM: may_apply仅表示返回时信封有效；消费前用同一helper核验内存连接摘要/绝对期限，逐题error仍由消费者处理，不授予写入权。
# 类用途: 明确区分关闭、观察、可用建议、到期、冷却和过期结果，失败时保留原业务方案。
@dataclass(frozen=True)
class DecisionOutcome:
    mode: str
    status: str
    response: DecisionResponse | None = None
    may_apply: bool = False
    reason: str = ""
    retry_after_seconds: float = 0.0
    retain_original: bool = True
    connection_revision: str = field(default="", repr=False)
    deadline: float = 0.0


# LLM: 身份只取宿主 params/runner；owner_background必须有run且无thread，不能从历史材料取身份或把活跃会话改称后台。
# 函数用途: 解析原会话或显式用户后台的准确身份，拒绝跨范围复用阶段。
def _identity(agent: object, params: object, *, scope: str = "thread") -> tuple[str, str, str, str]:
    if type(scope) is not str or scope not in {"thread", "owner_background"}:
        raise DecisionInputError("决策范围无效。")
    current = current_task_attributes(agent) or {}
    attrs = getattr(params, "task_attributes", {}) or {}
    if type(attrs) is not dict:
        raise DecisionInputError("决策需要可信任务属性。")
    supplied_thread = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id")
    direct_thread = getattr(params, "thread_id", "")
    if supplied_thread and direct_thread and supplied_thread != direct_thread:
        raise DecisionInputError("决策参数中的会话身份冲突。")
    result = []
    for active, supplied in (
        (current.get("agent_thread_id") or current.get("conversation_thread_id"), supplied_thread or direct_thread),
        (current_subagent_run_id(agent), getattr(params, "run_id", "")),
        (current.get("task_id"), getattr(params, "task_id", "") or attrs.get("task_id")),
    ):
        if active and supplied and active != supplied:
            raise DecisionInputError("决策任务身份与当前运行冲突。")
        value = active or supplied or ""
        if type(value) is not str or len(value) > 1024:
            raise DecisionInputError("决策任务身份格式无效。")
        result.append(value)
    if scope == "thread" and not result[0]:
        raise DecisionInputError("决策需要准确的当前会话。")
    if scope == "owner_background" and (result[0] or not result[1]):
        raise DecisionInputError("用户后台决策需要独立运行编号，不能借用会话身份。")
    return decision_owner_ref(agent), *result


# LLM: caller deadline 也是绝对 monotonic 时刻，不能接受 bool/无穷大，也不能把每层调用转换成新的相对预算。
# 函数用途: 校验可选调用方总期限。
def _deadline(value: object) -> float | None:
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise DecisionInputError("决策调用方期限无效。")
    return float(value)


# LLM: 用户停止优先于增强失败降级；复用原线程与工具取消事实，不按异常文本识别停止。
# 函数用途: 在准备、发送和结果消费边界传播原用户取消。
def _check_interrupted() -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError("当前决策随用户任务停止。")


# LLM: 开始时刻在读配置前取得；实验资格先于材料准备且不能重置总期限，当前可靠输入上界缺失时不发布可准备点。
# 函数用途: 建立原决策阶段；实验关闭、撤销或未具备发送合同均返回结构化原因，不调用后端或建立预算。
def begin_decision_stage(agent: object, params: object, *, operation_id: str, caller_deadline: float | None = None,
                         scope: str = "thread", experiment: bool = False) -> DecisionStage:
    started = time.monotonic()
    _check_interrupted()
    identity = ("", "", "", "")
    try:
        if type(operation_id) is not str or not operation_id or len(operation_id) > 1024 or type(experiment) is not bool:
            raise DecisionInputError("决策阶段需要宿主操作编号。")
        identity = _identity(agent, params, scope=scope)
        caller = _deadline(caller_deadline)
        settings = execute_decision_settings_operation(agent, "read", {}, thread_id=identity[1], blocking=False)
        budget = "background_timeout_seconds" if scope == "owner_background" else "stage_timeout_seconds"
        deadline = started + settings["effective"][budget]
        if caller is not None:
            deadline = min(deadline, caller)
        _check_interrupted()
        if experiment:
            from .decision_experiment import experiment_admission

            reason, experiment_deadline = experiment_admission(agent, params, settings)
            return DecisionStage(operation_id, *identity, started, min(deadline, experiment_deadline), reason,
                                 scope=scope, experiment=True)
        points = tuple(point for point, row in settings["effective"]["points"].items()
                       if row["effective_mode"] != "off" and POINT_RUNTIME_SCOPES[point] == scope)
        return DecisionStage(operation_id, *identity, started, deadline, scope=scope, enabled_points=points)
    except (InterruptedError, ToolCancelled):
        raise
    except Exception as exc:
        code = "settings_busy" if isinstance(exc, BlockingIOError) else "invalid_identity" if isinstance(exc, DecisionInputError) else "configuration_unavailable"
        return DecisionStage(operation_id if type(operation_id) is str else "", *identity, started, started, code, scope=scope,
                             experiment=experiment is True)


# LLM: 策略版本绑定两层CAS及实际有效值；连接摘要另外绑定，模型目录改动不能借未变的设置revision混过。
# 函数用途: 生成请求可携带的无秘密策略摘要。
def _policy_revision(settings: dict) -> str:
    value = {"revision": settings["revision"], "effective": settings["effective"]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# LLM: 读取原设置和原模型引用，不另存默认或凭据；off 在解析连接和构造后端之前返回。
# 函数用途: 冻结当前接入点的有效策略和已授权连接。
def _snapshot(agent: object, thread_id: str, point: str) -> tuple[dict, dict, str, dict | None]:
    settings = execute_decision_settings_operation(agent, "read", {}, thread_id=thread_id, blocking=False)
    row = settings["effective"]["points"][point]
    revision = _policy_revision(settings)
    if row["effective_mode"] == "off":
        return settings, row, revision, None
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    try:
        config = decision_profile(agent, data, row["profile_id"])
    except ModelProfileError:
        config = None
    return settings, row, revision, config


# LLM: 当前身份、设置和连接必须仍匹配发送快照；关闭/共享撤销/换密钥均不能应用旧建议。
# 函数用途: 在调用前后复读原事实源，返回旧请求是否已经失效。
def _stale(agent: object, params: object, stage: DecisionStage, point: str, revision: str, connection: str) -> str:
    if _identity(agent, params, scope=stage.scope) != (stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id):
        return "identity_changed"
    _settings, row, current_revision, config = _snapshot(agent, stage.thread_id, point)
    if row["effective_mode"] == "off":
        return "disabled"
    if config is None or current_revision != revision or connection_revision(config) != connection:
        return "policy_changed"
    return ""


# LLM: 实验 stage 在构造请求/后端前复读并失败关闭，不能凭伪造 stage 放行；普通点仍沿原 schema/身份/期限及调用账。
# 函数用途: 在合法范围请求普通建议；实验当前只返回资格或缺少输入证明的原因，保留原业务。
def decide(agent: object, params: object, stage: DecisionStage, *, point: str, state: object, questions: dict,
           candidates_revision: str, source_refs: tuple[str, ...] = (), caller_deadline: float | None = None,
           explicit_retry: bool = False) -> DecisionOutcome:
    _check_interrupted()
    mode = "off"
    try:
        if type(point) is not str or point not in POINT_RUNTIME_SCOPES or type(explicit_retry) is not bool:
            raise DecisionInputError("决策接入点或重试标志无效。")
        if not isinstance(stage, DecisionStage) or _identity(agent, params, scope=stage.scope) != (stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id):
            return DecisionOutcome(mode, "stale", reason="identity_changed")
        if stage.experiment:
            from .decision_experiment import experiment_admission

            settings = execute_decision_settings_operation(agent, "read", {}, thread_id=stage.thread_id, blocking=False)
            reason, _ = experiment_admission(agent, params, settings, point=point)
            return DecisionOutcome(mode, "off" if reason == "experiment_disabled" else "experiment_unavailable", reason=reason)
        if stage.error_code:
            return DecisionOutcome(mode, "error" if stage.error_code == "settings_busy" else "configuration_required", reason=stage.error_code)
        if POINT_RUNTIME_SCOPES[point] != stage.scope:
            raise DecisionInputError("决策接入点与本次运行范围不匹配。")
        started = time.monotonic()
        caller = _deadline(caller_deadline)
        settings, row, revision, config = _snapshot(agent, stage.thread_id, point)
        mode = row["effective_mode"]
        if mode == "off":
            return DecisionOutcome(mode, "off", reason="disabled")
        if config is None:
            return DecisionOutcome(mode, "configuration_required", reason="configuration_unavailable")
        deadline = min(stage.deadline, started + row["timeout_seconds"], caller if caller is not None else stage.deadline)
        if time.monotonic() >= deadline:
            return DecisionOutcome(mode, "deadline", reason="budget_exhausted")
        connection = connection_revision(config)
        key = stage.owner_ref, row["profile_id"], connection
        blocked, remaining = cooldown_state(key, revision, retry=explicit_retry)
        if blocked:
            return DecisionOutcome(mode, blocked, reason="connection_backoff", retry_after_seconds=remaining)
        binding = DecisionBinding(point, stage.owner_ref, stage.operation_id, revision, candidates_revision,
            stage.thread_id, stage.run_id, stage.task_id, source_refs)
        request = DecisionRequest(binding, state, questions)
        backend = decision_backend_from_profile(config)
        active = ActiveDecision(stage.owner_ref, stage.thread_id, point, InterruptHandle(), agent, settings)
        token = uuid.uuid4().hex
        if not register_active(token, active):
            return DecisionOutcome(mode, "error", reason="notification_capacity")
        try:
            return _invoke(params, stage, request, backend, deadline, key, active)
        finally:
            unregister_active(token, active)
    except (InterruptedError, ToolCancelled):
        raise
    except ModelProfileError:
        return DecisionOutcome(mode, "configuration_required", reason="configuration_unavailable")
    except BlockingIOError:
        return DecisionOutcome(mode, "error", reason="settings_busy")
    except Exception as exc:
        return DecisionOutcome(mode, "error", reason="invalid_input" if isinstance(exc, DecisionInputError) else "enhancement_failed")


# LLM: 注册取消后再次复查堵住关闭/启动竞态；用户中断必须传播，设置取消仅使建议失效，不能冒充用户停止。
# 连接一返回响应就复位该连接的退避阶梯（进程内冷却表），之后的复核失败不算连接故障。
# 函数用途: 调用原模型边界并核验响应绑定、摘要、请求模型和最新配置；不执行任何业务变更。
def _invoke(params, stage, request, backend, deadline, key, active) -> DecisionOutcome:
    from .decision_model_call import invoke_decision_model_call

    agent, point, revision = active.context, request.binding.point, request.binding.policy_revision
    mode = active.settings["effective"]["points"][point]["effective_mode"]
    try:
        stale = _stale(agent, params, stage, point, revision, key[2])
        if stale:
            return DecisionOutcome(mode, "stale", reason=stale)
        _check_interrupted()
        if time.monotonic() >= deadline:
            return DecisionOutcome(mode, "deadline", reason="budget_exhausted")
        response = invoke_decision_model_call(agent, params, request, backend, deadline=deadline,
            resource_key=("decision", stage.owner_ref, stage.thread_id, key[1], key[2]), interrupt_handle=active.handle)
        record_success(key)
        _check_interrupted()
        if active.settings_cancelled:
            return DecisionOutcome(mode, "stale", reason="settings_changed")
        if time.monotonic() >= deadline:
            return DecisionOutcome(mode, "deadline", reason="late_response")
        if not isinstance(response, DecisionResponse) or response.binding != request.binding or response.input_digest != request.input_digest or response.requested_model != backend.model_name:
            return DecisionOutcome(mode, "stale", reason="response_binding_mismatch")
        stale = _stale(agent, params, stage, point, revision, key[2])
        if stale:
            return DecisionOutcome(mode, "stale", reason=stale)
        _check_interrupted()
        if active.settings_cancelled:
            return DecisionOutcome(mode, "stale", reason="settings_changed")
        if time.monotonic() >= deadline:
            return DecisionOutcome(mode, "deadline", reason="late_validation")
        return DecisionOutcome(mode, "success", response, mode == "apply", connection_revision=key[2], deadline=deadline)
    except InterruptedError:
        _check_interrupted()
        if active.settings_cancelled:
            return DecisionOutcome(mode, "stale", reason="settings_changed")
        raise
    except ToolCancelled:
        raise
    except BoundedCallBusyError:
        return DecisionOutcome(mode, "error", reason="admission_busy")
    except Exception as exc:
        _check_interrupted()
        if active.settings_cancelled:
            return DecisionOutcome(mode, "stale", reason="settings_changed")
        if isinstance(exc, ModelProfileError):
            return DecisionOutcome(mode, "stale", reason="configuration_changed")
        if isinstance(exc, DecisionInputError):
            return DecisionOutcome(mode, "error", reason="invalid_input")
        if isinstance(exc.__cause__, ConcurrencyTimeout):
            return DecisionOutcome(mode, "error", reason="admission_busy")
        if isinstance(exc, BlockingIOError):
            return DecisionOutcome(mode, "error", reason="settings_busy")
        if not isinstance(exc, (ProviderConfigurationError, ProviderRecoverableError, BoundedCallTimeoutError, TimeoutError)):
            return DecisionOutcome(mode, "error", reason="enhancement_failed")
        status = record_failure(key, revision, exc)
        if isinstance(exc, (BoundedCallTimeoutError, ProviderTimeoutError, TimeoutError)):
            status = "deadline"
        elif status == "cooldown":
            status = "error"
        return DecisionOutcome(mode, status, reason="provider_failed")


# LLM: 消费者在刷新候选后共用此只读门；沿原非阻塞设置/身份复核，不联网、不重置期限，用户取消不能吞成可选失败。
# 函数用途: 在真正采用建议前检查关闭、配置更改和期限；失败时消费者保留当前合法基础方案。
def decision_outcome_is_current(agent: object, params: object, stage: DecisionStage, outcome: DecisionOutcome) -> bool:
    _check_interrupted()
    try:
        if not outcome.may_apply or outcome.response is None or not outcome.connection_revision:
            return False
        binding = outcome.response.binding
        if (binding.owner_ref, binding.thread_id, binding.run_id, binding.task_id, binding.operation_id) != (
            stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id, stage.operation_id,
        ):
            return False
        deadline = min(stage.deadline, outcome.deadline)
        if time.monotonic() >= deadline:
            return False
        stale = _stale(agent, params, stage, binding.point, binding.policy_revision, outcome.connection_revision)
        _check_interrupted()
        return not stale and time.monotonic() < deadline
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        return False
