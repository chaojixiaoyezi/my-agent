
from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread

from ..backends.errors import ProviderTimeoutError
from ._runtime_params import ToolLoopExecuteParams
from .model.call_runtime import (
    effective_model_request_timeout_seconds as _effective_model_request_timeout_seconds,
)
from .model.call_runtime import (
    observed_chunk_filter,
    record_model_call_finished,
    record_model_call_timeout,
    start_model_call_record,
)
from .model.context_pressure import (
    context_pressure_response,
    is_context_window_error,
    mark_tool_context_digest_consumed,
    preflight_context_pressure_response,
)
from .runner.stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
)
from .tool_stream import (
    CompleteToolCallStreamAbort,
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    complete_tool_call_abort_response,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
    malformed_tool_protocol_abort_response,
)


@dataclass(frozen=True)
class ModelGenerateParams:
    agent: object
    params: ToolLoopExecuteParams
    prompt: str
    tool_rounds: int


@dataclass(frozen=True)
class _BackendGenerateResult:
    response: object | None = None
    exc: BaseException | None = None


@dataclass(frozen=True)
class _ProviderTimeoutRecord:
    request: ModelGenerateParams
    ledger: object
    call_id: str
    first_token_timeout_seconds: float
    exc: ProviderTimeoutError


@dataclass(frozen=True)
class _ModelGenerationState:
    chunk_filter: ToolBoundaryChunkFilter
    ledger: object
    call_id: str
    first_token_timeout_seconds: float
    on_chunk: object


def generate_model_response(request: ModelGenerateParams):
    if preflight := preflight_context_pressure_response(request):
        return preflight
    _trace_model_start(request)
    state = _start_model_generation(request)
    response = _generate_or_recover_context_pressure(request, state)
    return _finish_model_generation(request, state, response)


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


def _trace_model_start(request: ModelGenerateParams) -> None:
    trace_runner_model_request_started(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            prompt=request.prompt,
        )
    )


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
    mark_tool_context_digest_consumed(request.params)
    return response


def _build_tool_boundary_chunk_filter(request: ModelGenerateParams) -> ToolBoundaryChunkFilter:
    return ToolBoundaryChunkFilter(
        request.params.effective_on_chunk,
        max_inline_content_chars=_tool_write_inline_max_chars(request.agent),
    )


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


def _trace_model_failure(request: ModelGenerateParams, exc: BaseException) -> None:
    trace_runner_model_request_failed(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            exc=exc,
        )
    )


def _apply_tool_boundary_cut(request: ModelGenerateParams, response):
    response, cut = cut_response_after_first_complete_tool_call(response)
    if cut:
        request.params.tool_context.append(
            "[tool-system]\n"
            "模型回复在第一个完整工具调用后仍继续输出内容；系统已只保留第一个工具调用，"
            "后续正文不会作为工具结果、事实或下一轮上下文。"
        )
    return response


def _generate_with_wall_timeout(
    request: ModelGenerateParams, on_chunk, first_token_timeout_seconds: float = 0.0
):
    timeout = _effective_model_request_timeout_seconds(request.agent, first_token_timeout_seconds)
    if timeout <= 0:
        return request.agent.backend.generate(request.prompt, on_chunk=on_chunk)

    results: Queue[_BackendGenerateResult] = Queue(maxsize=1)

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


def _tool_write_inline_max_chars(agent: object) -> int | None:
    return getattr(getattr(agent, "config", None), "tool_write_inline_max_chars", None)
