# LLM: Tool-loop model generation tracing stays outside the loop service to keep orchestration flow small.
# 模块用途: 包装 backend.generate，并记录 runner 模型请求阶段 trace。

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread

from ..backends.errors import ProviderTimeoutError
from ._runtime_params import ToolLoopExecuteParams
from .model_call_runtime import (
    effective_model_request_timeout_seconds as _effective_model_request_timeout_seconds,
)
from .model_call_runtime import (
    observed_chunk_filter,
    record_model_call_finished,
    record_model_call_timeout,
    start_model_call_record,
)
from .model_context_pressure import (
    context_pressure_response,
    is_context_window_error,
    preflight_context_pressure_response,
)
from .runner_stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
)
from .tool_stream_boundary import (
    CompleteToolCallStreamAbort,
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    complete_tool_call_abort_response,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
    malformed_tool_protocol_abort_response,
)


# LLM: ModelGenerateParams bundles backend generation inputs for trace and bundle-interface guard.
# 类用途: 模型生成参数包，集中 agent、运行参数、prompt 和当前工具轮次。
@dataclass(frozen=True)
class ModelGenerateParams:
    agent: object
    params: ToolLoopExecuteParams
    prompt: str
    tool_rounds: int


# LLM: _BackendGenerateResult keeps the background timeout bridge typed without exposing thread details.
# 类用途: 保存 backend.generate 的返回或异常；公共模型调用边界用它把后台结果安全传回主流程。
@dataclass(frozen=True)
class _BackendGenerateResult:
    response: object | None = None
    exc: BaseException | None = None


# LLM: _ProviderTimeoutRecord bundles timeout trace facts to keep helper interfaces small.
# 类用途: 保存 provider_wall 超时记录需要的 request、ledger、call_id 和异常对象。
@dataclass(frozen=True)
class _ProviderTimeoutRecord:
    request: ModelGenerateParams
    ledger: object
    call_id: str
    first_token_timeout_seconds: float
    exc: ProviderTimeoutError


# LLM: _ModelGenerationState carries per-call ledger and stream-filter state through the wrapper.
# 类用途: 保存一次模型请求的 chunk_filter、账本、call_id、首 token 预算和 on_chunk 回调。
@dataclass(frozen=True)
class _ModelGenerationState:
    chunk_filter: ToolBoundaryChunkFilter
    ledger: object
    call_id: str
    first_token_timeout_seconds: float
    on_chunk: object


# LLM: generate_model_response wraps backend calls with refs-only runner stage trace events.
# 函数用途: 在模型请求前后写 runner 阶段心跳；普通主代理没有 runner id 时不会写 trace。
def generate_model_response(request: ModelGenerateParams):
    if preflight := preflight_context_pressure_response(request):
        return preflight
    _trace_model_start(request)
    state = _start_model_generation(request)
    response = _generate_or_recover_context_pressure(request, state)
    return _finish_model_generation(request, state, response)


# LLM: _generate_or_recover_context_pressure keeps the public model generation wrapper small.
# 函数用途: 执行 provider 请求，并把流式中断、超时和上下文撞墙统一转成稳定响应或异常。
def _generate_or_recover_context_pressure(request: ModelGenerateParams, state: _ModelGenerationState):
    try:
        return _generate_with_wall_timeout(
            request,
            state.on_chunk,
            state.first_token_timeout_seconds,
        )
    except CompleteToolCallStreamAbort as exc:
        return complete_tool_call_abort_response(
            exc,
            backend=str(getattr(request.agent.backend, "name", "") or ""),
        )
    except LongToolContentStreamAbort as exc:
        return long_write_abort_response(
            exc,
            backend=str(getattr(request.agent.backend, "name", "") or ""),
        )
    except MalformedToolProtocolStreamAbort as exc:
        return malformed_tool_protocol_abort_response(
            exc,
            backend=str(getattr(request.agent.backend, "name", "") or ""),
        )
    except ProviderTimeoutError as exc:
        _record_provider_timeout(_provider_timeout_record(request, state, exc))
        raise
    except Exception as exc:
        if is_context_window_error(exc):
            return context_pressure_response(
                request,
                source="provider_error",
                prompt_tokens=0,
                detail=str(exc),
            )
        _trace_model_failure(request, exc)
        raise


# LLM: _provider_timeout_record packages timeout facts for a single tracing call.
# 函数用途: 把模型请求超时所需的 request、ledger、call_id 和异常打包，避免超时记录接口膨胀。
def _provider_timeout_record(
    request: ModelGenerateParams,
    state: _ModelGenerationState,
    exc: ProviderTimeoutError,
) -> _ProviderTimeoutRecord:
    return _ProviderTimeoutRecord(
        request=request,
        ledger=state.ledger,
        call_id=state.call_id,
        first_token_timeout_seconds=state.first_token_timeout_seconds,
        exc=exc,
    )


# LLM: _trace_model_start isolates runner trace setup from model-call control flow.
# 函数用途: 记录模型请求开始事件；无 runner id 时底层 trace 函数会跳过。
def _trace_model_start(request: ModelGenerateParams) -> None:
    trace_runner_model_request_started(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            prompt=request.prompt,
        )
    )


# LLM: _start_model_generation creates ledger and stream observer state before the provider call.
# 函数用途: 初始化模型调用账本、首 token 预算和工具边界流式过滤器。
def _start_model_generation(request: ModelGenerateParams) -> _ModelGenerationState:
    chunk_filter = _build_tool_boundary_chunk_filter(request)
    ledger, call_id, first_token_estimate = start_model_call_record(request)
    on_chunk = observed_chunk_filter(
        ledger=ledger,
        call_id=call_id,
        chunk_filter=chunk_filter,
        first_token_estimate=first_token_estimate,
    )
    return _ModelGenerationState(
        chunk_filter=chunk_filter,
        ledger=ledger,
        call_id=call_id,
        first_token_timeout_seconds=first_token_estimate.timeout_seconds,
        on_chunk=on_chunk,
    )


# LLM: _finish_model_generation records success facts before returning the final response.
# 函数用途: 写 finished 账本、结束 chunk 过滤、截断多余工具后正文，并写响应 trace。
def _finish_model_generation(request: ModelGenerateParams, state: _ModelGenerationState, response):
    record_model_call_finished(state.ledger, state.call_id, response)
    state.chunk_filter.finish()
    response = _apply_tool_boundary_cut(request, response)
    trace_runner_model_response_received(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            response=response,
        )
    )
    return response


# LLM: _build_tool_boundary_chunk_filter centralizes stream filtering around tool-call boundaries.
# 函数用途: 创建流式输出过滤器，限制模型把超长 write 内容塞进单次工具调用。
def _build_tool_boundary_chunk_filter(request: ModelGenerateParams) -> ToolBoundaryChunkFilter:
    return ToolBoundaryChunkFilter(
        request.params.effective_on_chunk,
        max_inline_content_chars=_tool_write_inline_max_chars(request.agent),
    )


# LLM: _record_provider_timeout keeps timeout ledger facts and runner trace in the same branch.
# 函数用途: 记录模型请求总时长超时，并写 runner 失败 trace。
def _record_provider_timeout(record: _ProviderTimeoutRecord) -> None:
    record_model_call_timeout(
        ledger=record.ledger,
        call_id=record.call_id,
        timeout_seconds=_effective_model_request_timeout_seconds(
            record.request.agent,
            record.first_token_timeout_seconds,
        ),
        timeout_stage="provider_wall",
    )
    _trace_model_failure(record.request, record.exc)


# LLM: _trace_model_failure is shared by timeout and generic model-call failures.
# 函数用途: 用统一 runner stage trace 记录模型请求失败。
def _trace_model_failure(request: ModelGenerateParams, exc: BaseException) -> None:
    trace_runner_model_request_failed(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            exc=exc,
        )
    )


# LLM: _apply_tool_boundary_cut turns extra prose after a complete tool call into a prompt note only.
# 函数用途: 截断首个完整工具调用后的多余模型输出，避免自然语言变成工具事实来源。
def _apply_tool_boundary_cut(request: ModelGenerateParams, response):
    response, cut = cut_response_after_first_complete_tool_call(response)
    if cut:
        request.params.tool_context.append(
            "[tool-system]\n"
            "模型回复在第一个完整工具调用后仍继续输出内容；系统已只保留第一个工具调用，"
            "后续正文不会作为工具结果、事实或下一轮上下文。"
        )
    return response


# LLM: _generate_with_wall_timeout prevents a stuck provider call from trapping the whole runner.
# 函数用途: 给任意 backend.generate 增加 request_timeout 总时长保护；后端正常返回时保持原响应对象。
def _generate_with_wall_timeout(
    request: ModelGenerateParams, on_chunk, first_token_timeout_seconds: float = 0.0
):
    timeout = _effective_model_request_timeout_seconds(request.agent, first_token_timeout_seconds)
    if timeout <= 0:
        return request.agent.backend.generate(request.prompt, on_chunk=on_chunk)

    results: Queue[_BackendGenerateResult] = Queue(maxsize=1)

    # LLM: _target runs the provider call in a daemon guard thread so the caller can regain control.
    # 函数用途: 执行真实模型请求并把返回值或异常放回队列；超时后线程不会阻塞当前 run 退出。
    def _target() -> None:
        try:
            results.put(
                _BackendGenerateResult(
                    response=_generate_backend_response(request, on_chunk, timeout)
                )
            )
        except BaseException as exc:  # pragma: no cover - exercised through queue result.
            results.put(_BackendGenerateResult(exc=exc))

    worker = Thread(target=_target, name="my-agent-model-generate-timeout-guard", daemon=True)
    worker.start()
    try:
        result = results.get(timeout=timeout)
    except Empty as exc:
        raise ProviderTimeoutError(f"模型接口请求超时: request_timeout={timeout:g}s") from exc
    if result.exc is not None:
        raise result.exc
    return result.response


# LLM: _generate_backend_response applies the same dynamic timeout to backend HTTP deadlines.
# 函数用途: 临时提升 backend.request_timeout，使内部流式 SSE deadline 与外层总墙使用同一结构化预算。
def _generate_backend_response(request: ModelGenerateParams, on_chunk, timeout: float):
    backend = request.agent.backend
    original = getattr(backend, "request_timeout", None)
    if timeout <= 0 or original is None:
        return backend.generate(request.prompt, on_chunk=on_chunk)
    try:
        effective = max(float(original), float(timeout))
    except (TypeError, ValueError):
        effective = float(timeout)
    try:
        backend.request_timeout = effective
        return backend.generate(request.prompt, on_chunk=on_chunk)
    finally:
        backend.request_timeout = original


# LLM: _tool_write_inline_max_chars keeps streaming guard aligned with write_file config.
# 函数用途: 从 config 读取单次 inline 写入上限；无效值由工具内容策略回退默认值。
def _tool_write_inline_max_chars(agent: object) -> int | None:
    return getattr(getattr(agent, "config", None), "tool_write_inline_max_chars", None)
