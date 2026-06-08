
from __future__ import annotations

import time
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread

from ..backends import ModelResponse
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
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
    long_write_response_abort,
    malformed_tool_protocol_abort_response,
)

_TOOL_STREAM_POLL_SECONDS = 0.05
_TOOL_STREAM_COMPLETE_DRAIN_SECONDS = 1.2


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
            state,
            state.first_token_timeout_seconds,
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
    state.chunk_filter.finish()
    response = _recover_unclosed_long_write_response(request, response)
    record_model_call_finished(state.ledger, state.call_id, response)
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


def _recover_unclosed_long_write_response(request: ModelGenerateParams, response):
    text = str(getattr(response, "text", "") or "")
    abort = long_write_response_abort(
        text,
        max_inline_content_chars=_tool_write_inline_max_chars(request) or 0,
    )
    if abort is None:
        return response
    backend = str(getattr(response, "backend", "") or getattr(request.agent.backend, "name", "") or "")
    return long_write_abort_response(abort, backend=backend)


def _build_tool_boundary_chunk_filter(request: ModelGenerateParams) -> ToolBoundaryChunkFilter:
    return ToolBoundaryChunkFilter(
        request.params.effective_on_chunk,
        max_inline_content_chars=_tool_write_inline_max_chars(request),
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
            "模型回复里同时包含工具调用和普通正文；系统已只保留完整工具/写入机器块，"
            "普通正文和伪造的内部记录不会作为工具结果、事实或下一轮上下文。"
        )
    return response


def _generate_with_wall_timeout(
    request: ModelGenerateParams,
    state: _ModelGenerationState,
    first_token_timeout_seconds: float = 0.0,
):
    timeout = _effective_model_request_timeout_seconds(request.agent, first_token_timeout_seconds)
    if timeout <= 0:
        return request.agent.backend.generate(request.prompt, on_chunk=state.on_chunk)

    results: Queue[_BackendGenerateResult] = Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put(
                _BackendGenerateResult(
                    response=_generate_backend_response(request, state.on_chunk, timeout)
                )
            )
        except BaseException as exc:  # pragma: no cover - exercised through queue result.
            results.put(_BackendGenerateResult(exc=exc))

    worker = Thread(target=_target, name="my-agent-model-generate-timeout-guard", daemon=True)
    worker.start()
    started = time.monotonic()
    tool_block_completed_at: float | None = None
    while True:
        now = time.monotonic()
        remaining = timeout - (now - started)
        if remaining <= 0:
            raise ProviderTimeoutError(f"模型接口请求超时: request_timeout={timeout:g}s")
        poll = min(_TOOL_STREAM_POLL_SECONDS, remaining)
        try:
            result = results.get(timeout=poll)
            break
        except Empty:
            tool_block_completed_at = _tool_block_completed_at(state, tool_block_completed_at)
            if _complete_tool_block_wait_elapsed(tool_block_completed_at):
                return _complete_stream_tool_response(request, state)
            continue
    if result.exc is not None:
        raise result.exc
    return result.response


def _tool_block_completed_at(state: _ModelGenerationState, current: float | None) -> float | None:
    if current is not None:
        return current
    if state.chunk_filter.cut_detected and state.chunk_filter.complete_tool_text():
        return time.monotonic()
    return None


def _complete_tool_block_wait_elapsed(completed_at: float | None) -> bool:
    if completed_at is None:
        return False
    return time.monotonic() - completed_at >= _TOOL_STREAM_COMPLETE_DRAIN_SECONDS


def _complete_stream_tool_response(request: ModelGenerateParams, state: _ModelGenerationState) -> ModelResponse:
    text = state.chunk_filter.complete_tool_text()
    backend = str(getattr(request.agent.backend, "name", "") or "")
    return ModelResponse(text=text, backend=backend)


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


def _tool_write_inline_max_chars(request: ModelGenerateParams) -> int | None:
    return getattr(getattr(request.agent, "config", None), "tool_write_inline_max_chars", None)
