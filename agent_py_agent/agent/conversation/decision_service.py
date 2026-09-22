# LLM: 可选决策只有建议权；阶段期限来自原业务batch，owner/thread设置与模型引用发送前后复核，消费者仍核验候选。
# 模块用途: 在原模型配置、原有界调用和原账本之外提供无业务写入权的决策策略边界。
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass

from ..agent_core.runner.context import current_subagent_run_id, current_task_attributes
from ..backends.base import BackendOptions
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
from ..backends.typesafe_decision import TypesafeDecisionBackend
from ..concurrency.interrupt import InterruptHandle, is_interrupted
from ..llm_scale.concurrency import ConcurrencyTimeout
from ..settings.decision_settings import execute_decision_settings_operation
from ..settings.decision_settings_projection import decision_profile
from ..settings.decision_settings_schema import POINTS
from ..settings.model_profiles import model_profiles_path, read_model_profiles
from ..settings.model_provider_schema import ModelProfileError
from ..tooling.cancellation import ToolCancelled, raise_if_cancelled
from .decision_policy import (
    ActiveDecision,
    connection_revision,
    cooldown_state,
    decision_owner_ref,
    record_failure,
    register_active,
    unregister_active,
)


# LLM: 由宿主在准备前创建一次，不拥有调度状态；修改配置不改变已冻结 deadline，不能跨身份复用。
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


# LLM: may_apply 仅表示信封和策略有效，不证明每道题成功，更不授予业务写入权；响应逐题 error 必须由消费者处理。
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


# LLM: 运行身份只取宿主 params/线程本地上下文；两者明确冲突时拒绝，不从 state/questions 选身份。
# 函数用途: 解析当前准确 thread/run/task，防止把一次请求或阶段转用到另一个会话。
def _identity(agent: object, params: object) -> tuple[str, str, str, str]:
    current = current_task_attributes(agent) or {}
    attrs = getattr(params, "task_attributes", {}) or {}
    if type(attrs) is not dict:
        raise DecisionInputError("决策需要可信任务属性。")
    result = []
    for active, supplied in (
        (current.get("agent_thread_id") or current.get("conversation_thread_id"), attrs.get("agent_thread_id") or attrs.get("conversation_thread_id")),
        (current_subagent_run_id(agent), getattr(params, "run_id", "")),
        (current.get("task_id"), getattr(params, "task_id", "") or attrs.get("task_id")),
    ):
        if active and supplied and active != supplied:
            raise DecisionInputError("决策任务身份与当前运行冲突。")
        value = active or supplied or ""
        if type(value) is not str or len(value) > 1024:
            raise DecisionInputError("决策任务身份格式无效。")
        result.append(value)
    if not result[0]:
        raise DecisionInputError("决策需要准确的当前会话。")
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


# LLM: started_at 在读取配置前取得；原owner/thread锁均非阻塞，忙时跳过增强，不为配置读锁另起worker。
# 函数用途: 在额外输入准备前建立一次批次预算；配置错误只产生不可调用阶段，保留原业务方案。
def begin_decision_stage(agent: object, params: object, *, operation_id: str, caller_deadline: float | None = None) -> DecisionStage:
    started = time.monotonic()
    _check_interrupted()
    identity = ("", "", "", "")
    try:
        if type(operation_id) is not str or not operation_id or len(operation_id) > 1024:
            raise DecisionInputError("决策阶段需要宿主操作编号。")
        identity = _identity(agent, params)
        caller = _deadline(caller_deadline)
        settings = execute_decision_settings_operation(agent, "read", {}, thread_id=identity[1], blocking=False)
        deadline = started + settings["effective"]["stage_timeout_seconds"]
        if caller is not None:
            deadline = min(deadline, caller)
        _check_interrupted()
        return DecisionStage(operation_id, *identity, started, deadline)
    except (InterruptedError, ToolCancelled):
        raise
    except Exception as exc:
        code = "settings_busy" if isinstance(exc, BlockingIOError) else "invalid_identity" if isinstance(exc, DecisionInputError) else "configuration_unavailable"
        return DecisionStage(operation_id if type(operation_id) is str else "", *identity, started, started, code)


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
    if _identity(agent, params) != (stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id):
        return "identity_changed"
    _settings, row, current_revision, config = _snapshot(agent, stage.thread_id, point)
    if row["effective_mode"] == "off":
        return "disabled"
    if config is None or current_revision != revision or connection_revision(config) != connection:
        return "policy_changed"
    return ""


# LLM: 原配置被固定成 BackendOptions，TypeSafe 仍不实现 generate；构造无网络，采样字段不进入决策协议。
# 函数用途: 构造一次已授权决策连接的轻量后端快照。
def _backend(config: dict) -> TypesafeDecisionBackend:
    return TypesafeDecisionBackend(BackendOptions(api_base=config["api_base"], api_key=config["api_key"],
        model_name=config["model_name"], context_window_tokens=config["model_context_window_tokens"],
        custom_headers=config["model_custom_headers"], session_header=config["model_session_header"]))


# LLM: 此入口只返回建议；caller 独占逻辑收口，provider调用/原账本/有界worker全由原 decision_model_call 负责。
# 函数用途: 在批次剩余预算内执行一次可选决策，观察不应用，所有普通失败保留原方案。
def decide(agent: object, params: object, stage: DecisionStage, *, point: str, state: object, questions: dict,
           candidates_revision: str, source_refs: tuple[str, ...] = (), caller_deadline: float | None = None,
           explicit_retry: bool = False) -> DecisionOutcome:
    _check_interrupted()
    mode = "off"
    try:
        if type(point) is not str or point not in POINTS or type(explicit_retry) is not bool:
            raise DecisionInputError("决策接入点或重试标志无效。")
        if not isinstance(stage, DecisionStage) or _identity(agent, params) != (stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id):
            return DecisionOutcome(mode, "stale", reason="identity_changed")
        if stage.error_code:
            return DecisionOutcome(mode, "error" if stage.error_code == "settings_busy" else "configuration_required", reason=stage.error_code)
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
        backend = _backend(config)
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
        return DecisionOutcome(mode, "success", response, mode == "apply")
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
