# LLM: Model generation owns the timeout-guard thread, stream filtering, provider error normalization, and propagation of typed task interruption into that real transport thread.
# 模块用途: 统一调用模型，处理超时、流式输出和上下文压力，并让用户停止能真正传到模型连接。
from __future__ import annotations

import time
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread

from ..backends import ModelResponse
from ..backends.errors import ProviderTimeoutError
from ..backends.tool_ir import AssistantTurn, ToolResult
from .tool_ir_history import native_tool_ir_history
from ..concurrency.interrupt import (
    is_interrupted,
    register_interrupt_callback,
    set_interrupt,
)
from ..tooling.runtime_contracts import ToolChoice
from ._runtime_params import ToolLoopExecuteParams
from .model.call_runtime import (
    effective_model_request_timeout_seconds as _effective_model_request_timeout_seconds,
)
from .model.call_runtime import (
    model_call_ledger,
    observed_chunk_filter,
    record_model_call_failed,
    record_model_call_finished,
    record_model_call_timeout,
    record_model_provider_attempt,
    start_model_call_record,
)
from .model.context_pressure import (
    context_pressure_response,
    is_context_window_error,
    preflight_context_pressure_response,
)
from .native_tool_protocol import native_tool_use_active, resolve_native_tools
from .runner.stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
)
from .tool_ir_guidance import append_runtime_guidance_user_message
from .tool_stream import (
    LongToolContentStreamAbort,
    MalformedToolProtocolStreamAbort,
    ToolBoundaryChunkFilter,
    long_write_abort_response,
    long_write_response_abort,
    malformed_tool_protocol_abort_response,
)

_TOOL_STREAM_POLL_SECONDS = 0.05
_MODEL_INTERRUPT_DRAIN_SECONDS = 1.0


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
    tool_choice: ToolChoice | None = None
    # native 下由 IR 历史翻出的厂商原生 messages；text 协议为 None（走单条 user prompt）。
    messages: list[dict] | None = None


# LLM: 这是工具模型轮的统一生成入口；preflight compact、成本和完成追踪必须保持同一路径，变更要同步生成测试。
# 函数用途: 在上下文预检后调用模型，记录真实 native 工具使用和成本，再完成本轮响应归档。
def generate_model_response(request: ModelGenerateParams):
    if preflight := preflight_context_pressure_response(request):
        return preflight
    _trace_model_start(request)
    state = _start_model_generation(request)
    # 门槛5补证(维护记录): 返回与响应配对的 final_state——重试成功后
    # 用 attempt-2 的 state 收口(其 call_id 落 finished), 不落在 attempt-1。
    response, final_state = _generate_or_recover_context_pressure(request, state)
    _record_run_cost(request, response)  # 审计 #19/#2:真实 USD 成本累计到 owner/run 维度
    return _finish_model_generation(request, final_state, response)


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


def _generate_or_recover_context_pressure(
    request: ModelGenerateParams, state: _ModelGenerationState
) -> tuple[object, _ModelGenerationState]:
    """返回 (response, 与之配对的收口 state)。重试成功后是 attempt-2 的
    retry_state(其 call_id 由外层统一 finish 收口)——见 seq1545 补证。"""
    try:
        return (
            _generate_with_wall_timeout(
                request,
                state,
                state.first_token_timeout_seconds,
            ),
            state,
        )
    except LongToolContentStreamAbort as exc:
        return (
            long_write_abort_response(
                exc,
                backend=str(getattr(request.agent.backend, "name", "") or ""),
            ),
            state,
        )
    except MalformedToolProtocolStreamAbort as exc:
        return (
            malformed_tool_protocol_abort_response(
                exc,
                backend=str(getattr(request.agent.backend, "name", "") or ""),
            ),
            state,
        )
    except ProviderTimeoutError as exc:
        _record_provider_timeout(_provider_timeout_record(request, state, exc))
        # 门槛5: 超时后按结构化 tool ledger 判定至多重试一次(只重发模型调用,
        # 不重放工具); 资格不足或重试再超时则原样上抛(无第三次)。
        retried = _retry_once_after_timeout(request)
        if retried is not None:
            retried_response, retried_state = retried
            return retried_response, retried_state
        raise
    except Exception as exc:
        record_model_call_failed(state.ledger, state.call_id, exc)
        if is_context_window_error(exc):
            return (
                context_pressure_response(
                    request,
                    source="provider_error",
                    prompt_tokens=0,
                    detail=str(exc),
                ),
                state,
            )
        _trace_model_failure(request, exc)
        raise


def _retry_once_after_timeout(request: ModelGenerateParams):
    """门槛5: 超时后至多重试一次, 只重发模型调用不重放工具。

    资格(fail-closed): IR 最后一条 tool_use 已确认(有配对 ToolResult)或
    无工具才可重试; 孤儿 tool_use(截断/未确认/未知)一律不重试。每 logical
    turn 全局最多一次: 同 logical 的物理尝试数 >1(本次超时已是首次)时不重试
    ——重试再超时无第三次。重试走 start_model_call_record 开 attempt-2
    新 call_id, physical_attempt 记账证明工具效果不重复。

    返回 (response, retry_state): retry_state 是 attempt-2 的收口 state,
    由外层统一 finish(seq1545 补证——attempt-2 的 call_id 必须落 finished,
    不能停留在 started/first_token)。
    """
    if not _ir_last_tool_use_confirmed(request):
        return None
    if _logical_physical_attempt_count(request) > 1:
        return None
    retry_state = _start_model_generation(request)
    try:
        response = _generate_with_wall_timeout(
            request,
            retry_state,
            retry_state.first_token_timeout_seconds,
        )
        return response, retry_state
    except ProviderTimeoutError as exc:
        _record_provider_timeout(_provider_timeout_record(request, retry_state, exc))
        return None  # 无第三次


def _ir_last_tool_use_confirmed(request: ModelGenerateParams) -> bool:
    """IR 最后一条 tool_use 是否已确认(有配对 ToolResult)。

    结构化 tool ledger 判定(门槛5): 重试资格不靠消息形状。无工具或最后
    tool_use 已收到配对回执 -> True(可重试); 孤儿 tool_use(截断/未确认/
    未知) -> False(fail-closed 不重试)。IR 不可得时按无工具处理(不误杀)。
    """
    try:
        history = native_tool_ir_history(request.params)
    except Exception:
        return True
    last_call_id: str | None = None
    for item in history:
        if isinstance(item, AssistantTurn):
            for call in item.tool_calls:
                last_call_id = call.call_id  # 取最后一条 tool_use 的 id
        elif isinstance(item, ToolResult):
            if last_call_id is not None and item.call_id == last_call_id:
                return True  # 最后 tool_use 已收到配对回执
    return last_call_id is None  # 无工具 -> 可重试; 有孤儿 -> fail-closed


def _logical_physical_attempt_count(request: ModelGenerateParams) -> int:
    """同 logical turn 已落账的物理尝试数(门槛5 全局最多一次判定)。

    从最近落账记录(本次超时)的 metadata.logical_call_id 取当前 logical 分组,
    与 start_model_call_record 落账同源, 不重新推导。
    """
    try:
        ledger = model_call_ledger(request.agent)
        records = list(ledger.records())
        if not records:
            return 0
        logical_id = str(records[-1].metadata.get("logical_call_id") or "")
        if not logical_id:
            return 0
        return sum(
            1
            for record in records
            if str(record.metadata.get("logical_call_id") or "") == logical_id
        )
    except Exception:
        return 0


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
    _begin_model_turn_identity(request.params, request.tool_rounds)
    chunk_filter = _build_tool_boundary_chunk_filter(request)
    ledger, call_id, first_token_estimate = start_model_call_record(request)
    on_chunk = observed_chunk_filter(
        ledger=ledger,
        call_id=call_id,
        chunk_filter=chunk_filter,
        first_token_estimate=first_token_estimate,
    )
    tools = resolve_native_tools(request.agent, request.params)
    tool_choice = _model_turn_tool_choice(request.params, tools)
    _remember_model_turn_tool_choice(request.params, tool_choice)
    return _ModelGenerationState(
        chunk_filter=chunk_filter,
        ledger=ledger,
        call_id=call_id,
        first_token_timeout_seconds=first_token_estimate.timeout_seconds,
        on_chunk=on_chunk,
        tools=tools,
        tool_choice=tool_choice,
        messages=_native_provider_messages(request.agent, request.params),
    )


def _begin_model_turn_identity(params: object, tool_rounds: int) -> str:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return ""
    sequence = max(0, int(state.get("_model_turn_sequence") or 0)) + 1
    state["_model_turn_sequence"] = sequence
    turn_id = f"{getattr(params, 'run_id', '')}:model:{sequence}:after-tools:{tool_rounds}"
    state["_current_model_turn_id"] = turn_id
    return turn_id


def _native_provider_messages(agent: object, params: object) -> list[dict] | None:
    """native 下把 IR 历史和宿主运行时指引翻成厂商原生 messages。

    text 协议始终返回 ``None``。native 第一轮或协议修复轮可能没有 IR 工具
    历史，但只要 ``tool_context`` 有尚未转发的宿主指引，仍必须返回结构化
    user 消息。backend 会先放入原始 prompt，再追加这些指引。
    """
    if not native_tool_use_active(params):
        return None
    history = getattr(params, "tool_ir_history", None)
    from ..backends.message_adapter import AnthropicMessageAdapter, strip_orphaned_tool_blocks

    messages = AnthropicMessageAdapter().to_provider_messages(history) if history else []
    # Step 4 最后防线：发请求前再扫一遍孤儿（Step3 的整对回收漏了截断/异常中断/subagent
    # 提前结束/resume 等边界时，IR 仍可能残留「有 tool_use 无配对 tool_result」或反之）。
    # Anthropic 对孤儿一律 HTTP 400，这道 sweep 给孤儿 tool_use 补 stub、剔除孤儿
    # tool_result，保证出站永不带孤儿。
    messages = strip_orphaned_tool_blocks(messages)
    # 把 tool_context 里的非工具运行时指引作为收尾 user 文本消息接回 native
    # messages；IR 承载真实工具往返和 current-turn UserTurn，其他策略、运行事件与
    # 进度文本不会自动进入 IR。
    # 用跨轮持有的 seen 去重集合（存 live_archive_state，随 params 在工具循环里复用同一
    # 实例）走「全表未转发」口径：不只转发尾部，**夹在工具往返中间**、被后续 [tool-record]
    # 越过的指引（护栏/进度/deferred 通知等）也能到达
    # native 模型，靠精确文本去重保证每条只发一次，绝不逐轮重复。
    messages = append_runtime_guidance_user_message(
        messages,
        getattr(params, "tool_context", None),
        seen=_forwarded_guidance_seen(params),
    )
    return messages or None


def _model_turn_tool_choice(
    params: object,
    tools: list[dict] | None,
) -> ToolChoice:
    """Read the host-owned choice fixed for this turn; provider prose cannot alter it."""

    from ..contracts.required_actions import tool_choice_for_required_actions

    snapshot = getattr(params, "effective_contract_snapshot", None)
    if snapshot is not None:
        visible_tools = tools
        if visible_tools is None:
            runtime_snapshot = getattr(params, "tool_runtime_snapshot", None)
            visible_tools = [
                {"name": name}
                for name in sorted(getattr(runtime_snapshot, "available_tool_names", ()) or ())
            ]
        return tool_choice_for_required_actions(snapshot, visible_tools)
    state = getattr(params, "live_archive_state", None)
    candidate = state.get("tool_choice") if isinstance(state, dict) else None
    if isinstance(candidate, ToolChoice):
        return candidate
    return ToolChoice.auto("ordinary_tool_turn")


def _remember_model_turn_tool_choice(params: object, choice: ToolChoice) -> None:
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        state["_current_model_turn_tool_choice"] = choice
        trace = state.setdefault("tool_choice_trace", [])
        if isinstance(trace, list):
            trace.append(
                {
                    "turn_id": str(state.get("_current_model_turn_id") or ""),
                    "mode": choice.mode,
                    "tool_name": choice.tool_name,
                    "reason": choice.reason,
                }
            )


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


# LLM: 该收尾只完成 chunk/filter、模型账本和 trace；不得裁掉正文后把内嵌文本提升为工具调用。
# 函数用途: 统一收尾一次模型调用，并把最终可用响应交回工具循环。
def _finish_model_generation(request: ModelGenerateParams, state: _ModelGenerationState, response):
    state.chunk_filter.finish()
    response = _recover_unclosed_long_write_response(request, response)
    record_model_call_finished(state.ledger, state.call_id, response)
    trace_runner_model_response_received(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            response=response,
        )
    )
    return response


def _recover_unclosed_long_write_response(request: ModelGenerateParams, response):
    if native_tool_use_active(request.params):
        # native 直通：正文不裁决 text 协议标记（写内容走 tool_use blocks；
        # 正文提及 [TOOL_CALL] 由最终裁决 _native_prose_violation 处理）。
        return response
    text = str(getattr(response, "text", "") or "")
    abort = long_write_response_abort(
        text,
        max_inline_content_chars=_tool_write_inline_max_chars(request) or 0,
    )
    if abort is None:
        return response
    backend = str(
        getattr(response, "backend", "") or getattr(request.agent.backend, "name", "") or ""
    )
    return long_write_abort_response(abort, backend=backend)


def _build_tool_boundary_chunk_filter(request: ModelGenerateParams) -> ToolBoundaryChunkFilter:
    # native 直通：text parser 不解析 native 正文（裁决归 _native_prose_violation，
    # 仅完整伪调用成对才拦），防止正文【提及】marker 被流式中途误杀
    # （真机 2026-08-06 MiniMax 形态）。
    return ToolBoundaryChunkFilter(
        _model_chunk_callback(request.params.effective_on_chunk),
        max_inline_content_chars=_tool_write_inline_max_chars(request),
        bypass=native_tool_use_active(request.params),
    )


def _model_chunk_callback(on_chunk: object):
    """Prefer the typed model-delta sink while preserving plain callbacks."""
    writer = getattr(on_chunk, "write_model", None)
    return writer if callable(writer) else on_chunk


def _record_provider_timeout(record: _ProviderTimeoutRecord) -> None:
    record_model_call_timeout(
        ledger=record.ledger,
        call_id=record.call_id,
        timeout_seconds=_effective_model_request_timeout_seconds(
            record.request.agent,
            record.first_token_timeout_seconds,
        ),
        # 门槛2: stage 由异常携带（stream_idle/wall_clock/provider_declared），
        # getattr 兜底 provider_wall 兼容历史异常对象（门槛2 前无 stage 字段）。
        timeout_stage=str(getattr(record.exc, "stage", "") or "provider_wall"),
        elapsed_seconds=_provider_timeout_elapsed(record.ledger, record.call_id),
        idle_silence_seconds=_provider_timeout_idle_silence(
            record.ledger, record.call_id
        ),
    )
    _trace_model_failure(record.request, record.exc)


def _provider_timeout_idle_silence(ledger: object, call_id: str) -> float | None:
    """最后活动(last_activity_at)到掐断时刻的静默时长（门槛2 第三证据字段），
    只记秒数不混 token 延迟、不带 URL/prompt/响应内容。

    门槛2 终审边界③(seq1622-3): record 不在账或 ledger 异常时返回 None 而非
    0.0——与参数层合同一致(None=缺失/未计算, 数值含 0.0=真实计算), 不把
    fallback 与「真实零静默」合并(此前 fallback 0.0 会污染账本语义)。
    """
    try:
        for item in ledger.records():
            if item.call_id == call_id:
                return max(
                    0.0,
                    float(ledger.context.now()) - float(item.last_activity_at),
                )
    except Exception:
        pass
    return None


def _provider_timeout_elapsed(ledger: object, call_id: str) -> float:
    """掐断时刻的真实墙钟经过（now - record.started_at），与 record 层时间戳
    双持贯通证据链；record 不在账或 ledger 异常时返回 0.0（行为不回归）。"""
    try:
        for item in ledger.records():
            if item.call_id == call_id:
                return max(0.0, float(ledger.context.now()) - float(item.started_at))
    except Exception:
        pass
    return 0.0


def _trace_model_failure(request: ModelGenerateParams, exc: BaseException) -> None:
    trace_runner_model_request_failed(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            exc=exc,
        )
    )


# LLM: The caller owns the typed task identity while the provider runs in a guard thread; relay interruption to that child before returning.
# 函数用途: 用超时保护线程调模型，并把外层任务的停止信号转发给真正读模型响应的线程。
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
                _BackendGenerateResult(response=_generate_backend_response(request, state, timeout))
            )
        except BaseException as exc:  # pragma: no cover - exercised through queue result.
            results.put(_BackendGenerateResult(exc=exc))
        finally:
            set_interrupt(False)

    worker = Thread(target=_target, name="my-agent-model-generate-timeout-guard", daemon=True)
    worker.start()
    with register_interrupt_callback(lambda: _interrupt_generation_worker(worker)):
        return _wait_for_generation_result(request, state, results, worker, timeout)


# LLM: Poll the guard result at short safe points so an outer task stop wins over a late model result and drains the provider thread for a bounded interval.
# 函数用途: 等待模型线程返回；收到用户停止时先收回子线程的连接，再以中断结束当前轮。
def _wait_for_generation_result(
    request: ModelGenerateParams,
    state: _ModelGenerationState,
    results: Queue[_BackendGenerateResult],
    worker: Thread,
    timeout: float,
):
    started = time.monotonic()
    transport_owns_timeout = _transport_owns_stream_idle_timeout(request.agent)
    while True:
        if is_interrupted():
            _interrupt_generation_worker(worker)
            worker.join(timeout=_MODEL_INTERRUPT_DRAIN_SECONDS)
            raise InterruptedError("模型接口请求已被用户停止")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0 and not transport_owns_timeout:
            raise ProviderTimeoutError(
                f"模型接口请求超时: request_timeout={timeout:g}s",
                stage="wall_clock",
            )
        result = _poll_generation_result(
            results,
            _TOOL_STREAM_POLL_SECONDS if transport_owns_timeout else remaining,
        )
        if result is not None:
            if result.exc is not None:
                raise result.exc
            return result.response


def _transport_owns_stream_idle_timeout(agent: object) -> bool:
    backend = getattr(agent, "backend", None)
    return bool(
        getattr(backend, "stream_enabled", False)
        and getattr(backend, "stream_timeout_is_idle", False)
    )


# LLM: Target only the live timeout-guard thread; setting its flag invokes any provider response-close callback already registered there.
# 函数用途: 将停止精确转发给当前模型线程，触发其 HTTP/SSE 连接关闭回调。
def _interrupt_generation_worker(worker: Thread) -> None:
    if worker.is_alive() and worker.ident is not None:
        set_interrupt(True, worker.ident)


def _poll_generation_result(
    results: Queue[_BackendGenerateResult],
    remaining: float,
) -> _BackendGenerateResult | None:
    try:
        return results.get(timeout=min(_TOOL_STREAM_POLL_SECONDS, remaining))
    except Empty:
        return None


def _generate_backend_response(
    request: ModelGenerateParams, state: _ModelGenerationState, timeout: float
):
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
    from ..backends.gateway_helpers import provider_attempt_observer
    from ..llm_scale.hot_path import global_llm_admission_slot
    from ..observability.concurrency_metrics import llm_inflight
    from .model.llm_metrics import record_llm_call, record_llm_cost

    start = time.monotonic()
    label = type(backend).__name__
    model = str(getattr(backend, "model_name", "") or "")

    def _observe_provider_attempt(event: dict[str, object]) -> None:
        record_model_provider_attempt(state.ledger, state.call_id, event)

    # 全局在飞 LLM 并发闸(T4 层4):默认关=nullcontext 零变化;配了 LLM_MAX_INFLIGHT 才封顶,
    # 拿槽在 llm_inflight 计数【之前】(槽满时等待期不算在飞,gauge 只反映真在飞)。
    with provider_attempt_observer(_observe_provider_attempt):
        with global_llm_admission_slot():
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
        tool_choice = state.tool_choice or ToolChoice.auto()
        kwargs["tool_choice"] = tool_choice
        if tool_choice.mode != "auto":
            # LLM: 强制 tool_choice(specific/required/none)必须同时关思考——部分兼容端点
            # (如 工具运行时 zen)在思考模式下拒绝强制工具选择,回哑 400;不识别该字段的
            # 端点(如 MiniMax)静默忽略。与 generate_structured 的 thinking_disabled 同一形态。
            kwargs["thinking_disabled"] = True
    if state.messages is not None:
        kwargs["messages"] = state.messages
    return backend.generate(prompt, **kwargs)


def _tool_write_inline_max_chars(request: ModelGenerateParams) -> int | None:
    return getattr(getattr(request.agent, "config", None), "tool_write_inline_max_chars", None)
