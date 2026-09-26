# LLM: 普通入口沿原生 decide；实验入口先算经验上界、原账预留并签发单次发送许可，传输层硬门复核后才可联网。
# 原 worker/终态/普通调用行为保持；实验结算在终态写入后的 finally 中执行，失败不退未知占用。
# 模块用途: 把短决策接入原有界 worker 与模型调用账，超时及时返回并保留真实后台资源及迟到传输事实。
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Hashable
from functools import partial

from ..agent_core.model import llm_metrics
from ..agent_core.model.call_runtime import (
    model_call_ledger,
    record_model_call_finished,
    record_model_call_timeout,
    record_model_provider_attempt,
)
from ..backends.bounded_call import (
    BoundedCallBusyError,
    BoundedCallTimeoutError,
    call_with_deadline,
)
from ..backends.decision_protocol import DecisionInputError, DecisionRequest, DecisionResponse
from ..backends.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from ..backends.gateway_helpers import provider_attempt_observer
from ..backends.gateway_request_limits import remaining_deadline_seconds
from ..backends.provider_headers import provider_runtime_scope
from ..backends.provider_send_gate import ProviderSendRefused
from ..backends.typesafe_decision_wire import jev_empirical_input_bound
from ..concurrency.interrupt import InterruptHandle, is_interrupted
from ..contracts.model_call_budget import ModelCallBudgetError, SendPermitBinding
from ..contracts.model_call_ledger import (
    TIMEOUT_STAGES,
    ModelCallFailureParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from ..llm_scale.hot_path import global_llm_admission_slot
from ..memory_archive import estimate_tokens
from ..observability.concurrency_metrics import llm_inflight
from ..runtime_context import current_subagent_attempt_id
from ..settings.decision_experiment_schema import experiment_task_id
from .decision_send_permit import DecisionExperimentCall, issue_send_permit
from .model_metrics import publish_model_metrics


# LLM: 管理员关闭了本 owner 的 Jev 使用权（owner_admin_controls）；在建调用记录前抛出，不联网、不进连接退避。
#   决策服务映射为 off/admin_disabled，连接测试映射为明确的拒绝说明。
# 类用途: 表示当前 owner 已被管理员禁止使用决策模型。
class DecisionModelDisallowed(RuntimeError):
    reason = "admin_disabled"


# LLM: experiment=None 保持原普通调用；实验只接受决策服务构造的 DecisionExperimentCall，上界越界或预留失败时不建调用记录。
# 实验结果仍只是建议，调用方固定按 observe 处理；普通调用仍沿原绝对期限/准入/worker 且不重试。
# 这是所有 Jev 联网（普通、实验、连接测试）的唯一入口，管理员禁用硬门放在最前：每次现读 owner 策略，拒绝时不建调用记录。
# 函数用途: 执行一次可选决策并记原账；实验路径在联网前完成上界计算、预算预留和发送许可签发。
def invoke_decision_model_call(
    agent: object,
    params: object,
    request: DecisionRequest,
    backend: object,
    *,
    deadline: float,
    resource_key: Hashable,
    interrupt_handle: InterruptHandle | None = None,
    experiment: DecisionExperimentCall | None = None,
) -> DecisionResponse:
    from ..user_space.owner_admin_controls import owner_decision_model_allowed

    started_at = time.monotonic()
    if not isinstance(request, DecisionRequest) or not callable(getattr(backend, "decide", None)):
        raise DecisionInputError("决策调用需要原生请求与 decide 后端。")
    if not owner_decision_model_allowed(getattr(agent, "home_paths", None)):
        raise DecisionModelDisallowed("管理员已关闭当前用户的决策模型使用权。")
    if experiment is not None and type(experiment) is not DecisionExperimentCall:
        raise ModelCallBudgetError("experiment_call_invalid")
    ledger = model_call_ledger(agent)
    if experiment is None:
        record, caller_token = ledger.started_retained(_started_params(params, request, backend))
        operation = partial(backend.decide, request, deadline=deadline)
    else:
        record, caller_token, operation = _reserve_experiment(agent, params, request=request, backend=backend,
            ledger=ledger, experiment=experiment, deadline=deadline, handle=interrupt_handle)
    response = None
    succeeded = False
    with caller_token:
        try:
            response = call_with_deadline(
                lambda: _invoke_worker(agent, params, operation, ledger, record.call_id, deadline),
                deadline=deadline, resource_key=resource_key, interrupt_handle=interrupt_handle, optional=True,
            )
            _check_active(deadline, interrupt_handle)
            record_model_call_finished(ledger, record.call_id, response)
            succeeded = True
            return response
        except BaseException as exc:
            _record_error(ledger, record.call_id, exc, started_at=started_at, deadline=deadline)
            raise
        finally:
            _settle_experiment(ledger, record.call_id, experiment)
            _record_metrics(backend, time.monotonic() - started_at, response if succeeded else None)
            publish_model_metrics(agent, params, pending=False, usage_only=True)


# LLM: 顺序固定为无网络准备→经验上界→签发许可→原账预留，预留成功后不再有可失败步骤；越界或预留失败都不留调用记录。
# 许可绑定最终 wire 字节摘要、端点、模型与决策服务冻结的连接版本；调用记录的 input_tokens 仍是原估算。
# 函数用途: 为实验调用取得原调用记录、caller 保留句柄以及只允许携带许可发送的 worker 操作。
def _reserve_experiment(agent: object, params: object, *, request: DecisionRequest, backend: object,
                        ledger: ModelCallLedger, experiment: DecisionExperimentCall, deadline: float,
                        handle: InterruptHandle | None):
    if not callable(getattr(backend, "prepare", None)) or not callable(getattr(backend, "send", None)):
        raise ModelCallBudgetError("input_bound_unavailable")
    prepared = backend.prepare(request, deadline=deadline)
    bound = jev_empirical_input_bound(prepared.body, prepared.payload, point=request.binding.point)
    binding = SendPermitBinding(endpoint=prepared.endpoint, body_sha256=hashlib.sha256(prepared.body).hexdigest(),
                                model=prepared.model, connection_revision=experiment.connection_revision)
    started = _started_params(params, request, backend, experiment_identity=_experiment_identity(agent, params, request))
    permit = issue_send_permit(agent, params, ledger=ledger, call_id=started.call_id, experiment=experiment,
                               binding=binding, deadline=deadline, handle=handle)
    record, caller_token = ledger.reserve_input_budget(started, budget_id=experiment.authorization_id,
                                                       input_bound=bound, send_binding=binding)
    return record, caller_token, partial(backend.send, prepared, permit=permit)


# LLM: 预留身份取决策绑定中的准确 owner/thread/run/task 与当前 attempt/request，和授权信封用同一 task 投影；不读正文。
# 函数用途: 生成实验调用写入原记录、供原账同锁比较授权身份的 metadata。
def _experiment_identity(agent: object, params: object, request: DecisionRequest) -> dict:
    binding = request.binding
    return {"owner_ref": binding.owner_ref, "thread_id": binding.thread_id,
            "task_id": experiment_task_id(binding.task_id, binding.run_id),
            "attempt_id": current_subagent_attempt_id(agent) or str(getattr(params, "attempt_id", "") or ""),
            "decision_operation": "experiment_observe"}


# LLM: 结算只在终态写入后调用，普通调用直接返回；结算自身失败时保留预留占用（后续实验因 budget_busy 失败关闭），不改原调用结果。
#   原账返回的结算视图原样追加到实验调用的 settlements，供实验记录携带；结算失败只记 settlement_failed，不猜占用或用量。
# 函数用途: 按原记录事实结算实验预算，隔离结算异常，并把结算结果交回决策服务。
def _settle_experiment(ledger: ModelCallLedger, call_id: str, experiment: DecisionExperimentCall | None) -> None:
    if experiment is None:
        return
    try:
        settlement = ledger.settle_input_budget(call_id)
    except (ModelCallBudgetError, KeyError):
        logging.getLogger(__name__).warning("决策实验预算未能结算；原预留保持占用，后续实验不会继续发送。")
        settlement = {"call_id": call_id, "outcome": "settlement_failed"}
    experiment.settlements.append(settlement)


# LLM: 身份只读宿主 params 和冻结绑定；逻辑 ID 来自 operation+digest，真实模型取已配置 decision backend，正文及凭据不进 metadata。
# 实验调用额外写入授权比较所需的准确身份，run 取决策绑定；普通调用 metadata 与原先一致。
# 函数用途: 为原账本准备决策调用身份及输入估算，不借用主模型名称或生成请求内容。
def _started_params(params: object, request: DecisionRequest, backend: object, *,
                    experiment_identity: dict | None = None) -> ModelCallStartedParams:
    model = str(getattr(backend, "model_name", "") or "")
    logical = hashlib.sha256(f"{request.binding.operation_id}\0{request.input_digest}".encode()).hexdigest()
    attrs = getattr(params, "task_attributes", None) or {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or getattr(params, "thread_id", "") or "")
    metadata = {
        "purpose": "decision", "auxiliary": True, "logical_call_id": f"decision:{logical}",
        "thread_id": thread_id, "task_id": str(getattr(params, "task_id", "") or ""),
        "operation_id": request.binding.operation_id, "input_digest": request.input_digest,
    }
    run_id = str(getattr(params, "run_id", "") or "")
    if experiment_identity is not None:
        metadata.update(experiment_identity)
        run_id = request.binding.run_id
    return ModelCallStartedParams(
        call_id=f"decision:{logical}:{uuid.uuid4().hex}",
        backend=str(getattr(backend, "name", "") or ""), model=model,
        input_tokens=estimate_tokens(request.payload(model)),
        request_id=str(getattr(params, "request_id", "") or ""),
        run_id=run_id,
        metadata=metadata,
    )


# LLM: thread-local HTTP 观察必须在实际 worker 安装；先领取准确保留再检查取消，准入槽与保留仅在实际退出时释放。
# operation 由调用方绑定为普通 decide 或携带发送许可的 send，worker 不自行选择路径。
# 函数用途: 在原可选名额内执行一次决策请求，传播身份上下文并记录真实 HTTP 尝试，不提交逻辑终态。
def _invoke_worker(
    agent: object, params: object, operation: Callable[[], object],
    ledger: ModelCallLedger, call_id: str, deadline: float,
) -> DecisionResponse:
    try:
        worker_token = ledger.retain_call(call_id)
    except KeyError:
        _check_active(deadline)
        raise
    with worker_token:
        _check_active(deadline)

        # LLM: 回调只投影原传输的结构化事实，关闭缓存正文摘要；不能在 callback 中完成逻辑调用或持久化整个 scope。
        # 函数用途: 将 worker 所在线程的 HTTP 尝试追加到同一个调用记录。
        def observe(event: dict[str, object]) -> None:
            record_model_provider_attempt(ledger, call_id, event)

        with global_llm_admission_slot(optional=True), provider_runtime_scope(agent, params), provider_attempt_observer(observe, cache_diagnostics=False):
            _record_inflight(1)
            try:
                _check_active(deadline)
                response = operation()
                _check_active(deadline)
                if not isinstance(response, DecisionResponse):
                    raise ProviderResponseError("决策后端返回了不符合合同的响应。", error_code="DECISION_RESPONSE_INVALID")
                return response
            finally:
                _record_inflight(-1)


# LLM: 在途指标仅随实际 worker 进入和退出改变，caller 超时不能减计；遥测故障不能改变模型结果或资源释放。
# 函数用途: 沿原指标入口报告真实在途数，并隔离指标异常。
def _record_inflight(delta: int) -> None:
    try:
        llm_inflight(delta)
    except Exception:
        pass


# LLM: caller 只记录一次结果指标；决策请求不调用原价格估算或 USD 成本账，保留原调用账中的实际输入与请求终态。
# 函数用途: 记录决策请求的成功、失败、耗时和供应商用量；本地决策模型也不需要价格配置。
def _record_metrics(backend: object, elapsed: float, response: DecisionResponse | None) -> None:
    try:
        llm_metrics.record_llm_call(str(getattr(backend, "name", "") or ""), elapsed, response, ok=response is not None)
    except Exception:
        pass


# LLM: 用户/外部取消优先于局部期限；worker 内读取原精确句柄的线程事实，caller 额外读取宿主传入句柄，均不重置时间。
# 函数用途: 在发请求及采用响应前检查同一个取消事实和绝对截止时间。
def _check_active(deadline: float, handle: InterruptHandle | None = None) -> None:
    if is_interrupted() or (handle is not None and handle.cancelled):
        raise InterruptedError("当前决策调用已被取消。")
    remaining_deadline_seconds(deadline)


# LLM: 错误账只记录本地固定代码与类型，不复制异常正文/任意 error_code；BaseException 仍由原 caller 重新抛出。
# 函数用途: 将首次失败或超时写回原账本，保留用户中断及原异常分类供外层处理。
def _record_error(
    ledger: ModelCallLedger, call_id: str, exc: BaseException, *, started_at: float, deadline: float,
) -> None:
    if isinstance(exc, (BoundedCallTimeoutError, ProviderTimeoutError)):
        stage = exc.stage if isinstance(exc, ProviderTimeoutError) and exc.stage in TIMEOUT_STAGES else "wall_clock"
        record_model_call_timeout(ledger=ledger, call_id=call_id, timeout_seconds=max(0.0, deadline - started_at),
                                  timeout_stage=stage, elapsed_seconds=max(0.0, time.monotonic() - started_at))
        return
    ledger.failed(ModelCallFailureParams(call_id, type(exc).__name__, _error_code(exc)))


# LLM: 分类依据异常类型及 bounded 固定 reason，不解析错误文案、URL 或模型正文，不产生重试或恢复动作。
# 发送许可拒绝单独成码，账本据此与供应商失败区分，且不进入连接退避。
# 函数用途: 为决策失败生成可安全落账的错误代码。
def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (InterruptedError, KeyboardInterrupt, SystemExit)):
        return "DECISION_CANCELLED"
    if isinstance(exc, ProviderSendRefused):
        return "DECISION_SEND_REFUSED"
    if isinstance(exc, BoundedCallBusyError):
        return "DECISION_RESOURCE_BUSY" if exc.reason == "resource_busy" else "DECISION_CAPACITY_EXHAUSTED"
    if isinstance(exc, DecisionInputError):
        return "DECISION_INPUT_INVALID"
    if isinstance(exc, ProviderConfigurationError):
        return "DECISION_CONFIGURATION_INVALID"
    if isinstance(exc, ProviderResponseError):
        return "DECISION_RESPONSE_INVALID"
    return "DECISION_CALL_FAILED"


__all__ = ["DecisionModelDisallowed", "invoke_decision_model_call"]
