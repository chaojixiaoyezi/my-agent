# LLM: 此入口只执行普通原生 decide；实验缺可靠输入上界与发送硬门时在估算/建账前拒绝，原 worker/终态/普通调用行为保持。
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
from ..contracts.model_call_budget import ModelCallBudgetError
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


# LLM: 非空实验编号在任何输入估算或建账前失败关闭；该标签不是授权，普通调用仍沿原绝对期限/准入/worker 且不重试。
# 函数用途: 执行一次普通可选决策并记原账；当前实验入口没有可靠 provider-input 证明，不能发请求。
def invoke_decision_model_call(
    agent: object,
    params: object,
    request: DecisionRequest,
    backend: object,
    *,
    deadline: float,
    resource_key: Hashable,
    interrupt_handle: InterruptHandle | None = None,
    experiment_id: str = "",
) -> DecisionResponse:
    if type(experiment_id) is not str or experiment_id:
        raise ModelCallBudgetError("input_bound_unavailable")
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
            _record_metrics(backend, time.monotonic() - started_at, response if succeeded else None)
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
