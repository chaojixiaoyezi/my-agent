# LLM: 此入口只执行一次已配置的原生 decide；worker 持有原准入、身份头和 HTTP 观察，caller 决定原账本终态，不拥有设置、冷却或业务提交权。
# 模块用途: 把短决策接入原有界 worker 与模型调用账，超时及时返回并保留真实后台资源及迟到传输事实。
from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Hashable

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
from ..concurrency.interrupt import InterruptHandle, is_interrupted
from ..contracts.model_call_ledger import (
    TIMEOUT_STAGES,
    ModelCallFailureParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from ..llm_scale.hot_path import global_llm_admission_slot
from ..memory_archive import estimate_tokens
from ..observability.concurrency_metrics import llm_inflight
from .model_metrics import publish_model_metrics


# LLM: deadline 沿宿主准备阶段的绝对 monotonic 时刻；resource_key 由宿主提供稳定身份，禁止改用 backend id，异常原样交回外层分类且不重试。
# 函数用途: 执行一次可选决策并写入原调用账、刷新当前用量行；超时不释放 worker 的资源，不为显示等待会话写锁。
def invoke_decision_model_call(
    agent: object,
    params: object,
    request: DecisionRequest,
    backend: object,
    *,
    deadline: float,
    resource_key: Hashable,
    interrupt_handle: InterruptHandle | None = None,
) -> DecisionResponse:
    started_at = time.monotonic()
    if not isinstance(request, DecisionRequest) or not callable(getattr(backend, "decide", None)):
        raise DecisionInputError("决策调用需要原生请求与 decide 后端。")
    ledger = model_call_ledger(agent)
    record, caller_token = ledger.started_retained(_started_params(params, request, backend))
    response = None
    succeeded = False
    with caller_token:
        try:
            response = call_with_deadline(
                lambda: _invoke_worker(agent, params, request, backend, ledger, record.call_id, deadline),
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
            _record_metrics(agent, params, backend, time.monotonic() - started_at, response if succeeded else None)
            publish_model_metrics(agent, params, pending=False, usage_only=True)


# LLM: 身份只读宿主 params 和冻结绑定；逻辑 ID 来自 operation+digest，真实模型取已配置 decision backend，正文及凭据不进 metadata。
# 函数用途: 为原账本准备决策调用身份及输入估算，不借用主模型名称或生成请求内容。
def _started_params(params: object, request: DecisionRequest, backend: object) -> ModelCallStartedParams:
    model = str(getattr(backend, "model_name", "") or "")
    logical = hashlib.sha256(f"{request.binding.operation_id}\0{request.input_digest}".encode()).hexdigest()
    attrs = getattr(params, "task_attributes", None) or {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or getattr(params, "thread_id", "") or "")
    return ModelCallStartedParams(
        call_id=f"decision:{logical}:{uuid.uuid4().hex}",
        backend=str(getattr(backend, "name", "") or ""), model=model,
        input_tokens=estimate_tokens(request.payload(model)),
        request_id=str(getattr(params, "request_id", "") or ""),
        run_id=str(getattr(params, "run_id", "") or ""),
        metadata={
            "purpose": "decision", "auxiliary": True, "logical_call_id": f"decision:{logical}",
            "thread_id": thread_id, "task_id": str(getattr(params, "task_id", "") or ""),
            "operation_id": request.binding.operation_id, "input_digest": request.input_digest,
        },
    )


# LLM: thread-local HTTP 观察必须在实际 worker 安装；先领取准确保留再检查取消，准入槽与保留仅在实际退出时释放。
# 函数用途: 在原可选名额内调用原生 decide，传播身份上下文并记录真实 HTTP 尝试，不提交逻辑终态。
def _invoke_worker(
    agent: object, params: object, request: DecisionRequest, backend: object,
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
                response = backend.decide(request, deadline=deadline)
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


# LLM: caller 只记录一次结果指标；成本复用原模型定价，未知模型价格是内部保守估算而非供应商账单，不能写回权威 usage。
# 函数用途: 将决策结果与实际配置模型的估算成本送入原全局及 owner/run 指标，不建立价格表或显示账。
def _record_metrics(agent: object, params: object, backend: object, elapsed: float, response: DecisionResponse | None) -> None:
    try:
        llm_metrics.record_llm_call(str(getattr(backend, "name", "") or ""), elapsed, response, ok=response is not None)
        if response is not None:
            model = str(getattr(backend, "model_name", "") or "")
            llm_metrics.record_llm_cost(model, response)
            owner = str(getattr(getattr(agent, "config", None), "my_agent_owner_id", "") or "")
            llm_metrics.record_run_cost(owner, str(getattr(params, "run_id", "") or ""), model, response)
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
# 函数用途: 为决策失败生成可安全落账的错误代码。
def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (InterruptedError, KeyboardInterrupt, SystemExit)):
        return "DECISION_CANCELLED"
    if isinstance(exc, BoundedCallBusyError):
        return "DECISION_RESOURCE_BUSY" if exc.reason == "resource_busy" else "DECISION_CAPACITY_EXHAUSTED"
    if isinstance(exc, DecisionInputError):
        return "DECISION_INPUT_INVALID"
    if isinstance(exc, ProviderConfigurationError):
        return "DECISION_CONFIGURATION_INVALID"
    if isinstance(exc, ProviderResponseError):
        return "DECISION_RESPONSE_INVALID"
    return "DECISION_CALL_FAILED"


__all__ = ["invoke_decision_model_call"]
