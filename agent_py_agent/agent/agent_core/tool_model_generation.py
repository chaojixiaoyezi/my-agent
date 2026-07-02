
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
from .native_tool_protocol import native_tool_use_active, resolve_native_tools
from .tool_ir_guidance import append_runtime_guidance_user_message
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
    # 原生 tool_use 协议下传给 backend.generate 的 tools schema；text 协议为 None。
    tools: list[dict] | None = None
    # native 下由 IR 历史翻出的厂商原生 messages；text 协议为 None（走单条 user prompt）。
    messages: list[dict] | None = None


def generate_model_response(request: ModelGenerateParams):
    if preflight := preflight_context_pressure_response(request):
        return preflight
    _trace_model_start(request)
    state = _start_model_generation(request)
    # H3：digest 轮在 prompt 逼近窗口 90% 时由 preflight 触发，并把 live_archive_state 里的
    # digest_inflight/pending 标记置真。该轮若在生成中抛 ProviderTimeoutError 或一般异常，原先
    # 直接 re-raise、跳过 _finish_model_generation 里的 mark_tool_context_digest_consumed，标记
    # 永不清除→_has_pending_tool_context_digest 一直真→preflight 永远走 digest 分支、再不发
    # context_overflow→compact 永久卡死。这里用 try/finally 保证任何退出路径（含异常）都清理。
    # mark_tool_context_digest_consumed 本身以 inflight 标记为门：非 digest 轮它是 no-op，绝不
    # 误清正常轮，所以无条件兜底清理是安全的。
    try:
        response = _generate_or_recover_context_pressure(request, state)
        # 审计 #8:跟踪 native 空转(工具供给但 0 tool_use),连续 K 次自动降级 text(内部异常隔离)
        from .native_tool_protocol import record_native_turn

        record_native_turn(request.agent, bool(getattr(state, "tools", None)), response)
        _record_run_cost(request, response)  # 审计 #19/#2:真实 USD 成本累计到 owner/run 维度
        return _finish_model_generation(request, state, response)
    finally:
        mark_tool_context_digest_consumed(request.params)


def _record_run_cost(request: ModelGenerateParams, response: object) -> None:
    """采集 owner(config)/run_id(params)/model,记真实 USD 成本到全局台账(审计 #19/#2)。

    model 优先取 backend.model_name,缺则回退 config.model_name(部分后端不暴露 model_name 但 config 有)。
    """
    from .model.llm_metrics import record_run_cost

    config = getattr(request.agent, "config", None)
    owner = str(getattr(config, "my_agent_owner_id", "") or "")
    run_id = str(getattr(request.params, "run_id", "") or "")
    backend_model = getattr(getattr(request.agent, "backend", None), "model_name", "")
    model = str(backend_model or getattr(config, "model_name", "") or "")
    record_run_cost(owner, run_id, model, response)


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
        tools=resolve_native_tools(request.agent, request.params),
        messages=_native_provider_messages(request.agent, request.params),
    )


def _native_provider_messages(agent: object, params: object) -> list[dict] | None:
    """native 下把 IR 历史翻成厂商原生 messages；text 协议或空历史时返回 None。

    返回 None 时 backend 走单条 user=prompt 的旧路径（文本协议零改动）。native 的
    第一轮还没有任何工具往返时 IR 历史为空，也返回 None——此时整段 prompt 仍作为
    单条 user 消息发出，与现状一致；有往返后才切到结构化 messages。
    """
    if not native_tool_use_active(agent):
        return None
    history = getattr(params, "tool_ir_history", None)
    if not history:
        return None
    from ..backends.message_adapter import AnthropicMessageAdapter, strip_orphaned_tool_blocks

    messages = AnthropicMessageAdapter().to_provider_messages(history)
    # Step 4 最后防线：发请求前再扫一遍孤儿（Step3 的整对回收漏了截断/异常中断/subagent
    # 提前结束/resume 等边界时，IR 仍可能残留「有 tool_use 无配对 tool_result」或反之）。
    # Anthropic 对孤儿一律 HTTP 400，这道 sweep 给孤儿 tool_use 补 stub、剔除孤儿
    # tool_result，保证出站永不带孤儿。
    messages = strip_orphaned_tool_blocks(messages)
    # native 回归修复：把 tool_context 里「系统注入的运行时指引」（closeout 打回 /
    # 出口合同续修 / delivery 软提醒 / 问句逃逸守卫……）作为收尾 user 文本消息接到
    # 末尾。这类指引不是工具调用、不进 IR，又被 builder 的 native 旁路从 prompt 里
    # 整段丢掉——不接回来，native 模型永远收不到打回理由（弱模型写完 output 即停手、
    # closeout 判完成、续修轮蒙眼重复的根因）。孤儿净化只管 tool 块，指引在其后单独
    # 成一条 user 文本消息，Anthropic 允许连续 user 消息（合并为一轮）。
    # 用跨轮持有的 seen 去重集合（存 live_archive_state，随 params 在工具循环里复用同一
    # 实例）走「全表未转发」口径：不只转发尾部，**夹在工具往返中间**、被后续 [tool-record]
    # 越过的指引（delivery 软提醒/护栏/进度/deferred 通知/closeout 打回……）也能到达
    # native 模型，靠精确文本去重保证每条只发一次，绝不逐轮重复。
    messages = append_runtime_guidance_user_message(
        messages,
        getattr(params, "tool_context", None),
        seen=_forwarded_guidance_seen(params),
    )
    return messages or None


def _forwarded_guidance_seen(params: object) -> set:
    """返回跨轮持有的「已转发运行时指引」去重集合，挂在 ``live_archive_state`` 上。

    ``live_archive_state`` 是 ``ToolLoopExecuteParams`` 里 ``default_factory=dict`` 的
    可变字段，整个工具循环复用同一 params 实例，因此这个集合天然跨轮存活、无需新增
    dataclass 字段。拿不到 dict（伪 params/旧调用方）时退回一个一次性空集合——此时退化为
    单轮「全表未转发=全表」，仍不会重复，只是不跨轮记忆。
    """
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return set()
    seen = state.get("_forwarded_runtime_guidance")
    if not isinstance(seen, set):
        seen = set()
        state["_forwarded_runtime_guidance"] = seen
    return seen


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
        return _invoke_backend_generate(request.agent.backend, request.prompt, state)

    results: Queue[_BackendGenerateResult] = Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put(
                _BackendGenerateResult(
                    response=_generate_backend_response(request, state, timeout)
                )
            )
        except BaseException as exc:  # pragma: no cover - exercised through queue result.
            results.put(_BackendGenerateResult(exc=exc))

    worker = Thread(target=_target, name="my-agent-model-generate-timeout-guard", daemon=True)
    worker.start()
    started = time.monotonic()
    tool_block_completed_at: float | None = None
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ProviderTimeoutError(f"模型接口请求超时: request_timeout={timeout:g}s")
        result, tool_block_completed_at = _poll_generation_result(results, state, tool_block_completed_at, remaining)
        if result is not None:
            break
        if _complete_tool_block_wait_elapsed(tool_block_completed_at):
            return _complete_stream_tool_response(request, state)
    if result.exc is not None:
        raise result.exc
    return result.response


def _poll_generation_result(
    results: Queue[_BackendGenerateResult],
    state: _ModelGenerationState,
    tool_block_completed_at: float | None,
    remaining: float,
) -> tuple[_BackendGenerateResult | None, float | None]:
    try:
        return results.get(timeout=min(_TOOL_STREAM_POLL_SECONDS, remaining)), tool_block_completed_at
    except Empty:
        return None, _tool_block_completed_at(state, tool_block_completed_at)


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


def _generate_backend_response(request: ModelGenerateParams, state: _ModelGenerationState, timeout: float):
    backend = request.agent.backend
    original = getattr(backend, "request_timeout", None)
    if timeout <= 0 or original is None:
        return _invoke_backend_generate(backend, request.prompt, state)
    try:
        effective = max(float(original), float(timeout))
    except (TypeError, ValueError):
        effective = float(timeout)
    try:
        backend.request_timeout = effective
        return _invoke_backend_generate(backend, request.prompt, state)
    finally:
        backend.request_timeout = original


def _invoke_backend_generate(backend, prompt: str, state: _ModelGenerationState):
    # LLM 热路径 RED + token + USD 成本埋点(审计 #19):计时 + 成败 + token + cost 发到默认
    # registry,/metrics 暴露。record_llm_call/record_llm_cost 内部异常隔离,绝不影响下面真实调用。
    # llm_inflight(§6-A2):在飞 LLM 并发 gauge——"1000 用户扇出成多少并发模型调用"的实测值。
    from ..observability.concurrency_metrics import llm_inflight
    from .model.llm_metrics import record_llm_call, record_llm_cost

    start = time.monotonic()
    label = type(backend).__name__
    model = str(getattr(backend, "model_name", "") or "")
    llm_inflight(1)
    try:
        response = _do_backend_generate(backend, prompt, state)
    except Exception:
        record_llm_call(label, time.monotonic() - start, None, ok=False)
        raise
    finally:
        llm_inflight(-1)
    record_llm_call(label, time.monotonic() - start, response, ok=True)
    record_llm_cost(model, response)  # 真实 USD 成本按 model 累计(审计 #19 残余)
    return response


def _do_backend_generate(backend, prompt: str, state: _ModelGenerationState):
    # text 协议(tools/messages 均为 None)保持原调用形态，不传新关键字，旁路/伪后端零改动。
    if state.tools is None and state.messages is None:
        return backend.generate(prompt, on_chunk=state.on_chunk)
    kwargs: dict[str, object] = {"on_chunk": state.on_chunk}
    if state.tools is not None:
        kwargs["tools"] = state.tools
    if state.messages is not None:
        kwargs["messages"] = state.messages
    return backend.generate(prompt, **kwargs)


def _tool_write_inline_max_chars(request: ModelGenerateParams) -> int | None:
    return getattr(getattr(request.agent, "config", None), "tool_write_inline_max_chars", None)
