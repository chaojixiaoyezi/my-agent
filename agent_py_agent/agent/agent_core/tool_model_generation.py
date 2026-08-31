# LLM: Model generation owns the timeout-guard thread, stream filtering, provider error normalization, and propagation of typed task interruption into that real transport thread.
# 模块用途: 统一调用模型，处理超时、流式输出和上下文压力，并让用户停止能真正传到模型连接。
from __future__ import annotations

import os
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from queue import Empty, Queue
from threading import Thread

from ..backends import ModelResponse, ProviderRequestOptions
from ..backends.errors import ProviderTimeoutError
from ..backends.tool_ir import AssistantTurn, ToolResult
from ..concurrency.interrupt import (
    is_interrupted,
    register_interrupt_callback,
    set_interrupt,
)
from ..model_guidance import provider_system_instruction
from ..prompting_parts.cache_layout import CacheStructuredPrompt, prompt_cache_layout
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
    model_visible_context_snapshot,
    preflight_context_pressure_response,
    record_provider_context_observation,
)
from .native_tool_protocol import native_tool_use_active, resolve_native_tools
from .runner.stage_trace import (
    RunnerModelStageTraceRequest,
    RunnerModelStreamActivityTraceRequest,
    RunnerProviderRetryTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
    trace_runner_model_stream_active,
    trace_runner_provider_retry_scheduled,
)
from .runtime.conversation_state import conversation_runtime_state_section
from .tool_ir_guidance import unforwarded_runtime_guidance
from .tool_ir_history import native_tool_ir_history, record_runtime_facts_turn_ir
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
_RUNNER_STREAM_ACTIVITY_STATE_KEY = "_runner_model_stream_activity_projection"


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


# LLM: 一次物理模型调用的状态同时持有 filtered model delta 与原始 typed retry sink；两者不可互相冒充。
# 类用途: 保存单次模型调用的账本、流输出、重连显示出口和 native 工具参数。
@dataclass(frozen=True)
class _ModelGenerationState:
    agent: object
    params: ToolLoopExecuteParams
    chunk_filter: ToolBoundaryChunkFilter
    ledger: object
    call_id: str
    tool_rounds: int
    first_token_timeout_seconds: float
    on_chunk: object
    retry_sink: object
    # 原生 tool_use 协议下传给 backend.generate 的 tools schema；text 协议为 None。
    tools: list[dict] | None = None
    tool_choice: ToolChoice | None = None
    # native 下由 IR 历史翻出的厂商原生 messages；text 协议为 None（走单条 user prompt）。
    messages: list[dict] | None = None
    # 供应商真实 system/developer 通道使用的稳定宿主规则；不混入用户任务或 IR 历史。
    system_instruction: str = ""
    # 仅用于富 TUI 的人类可读耗时；不参与模型 timeout、重试或工具状态判断。
    started_at: float = 0.0
    # provider block-stop observer 已发布的 typed 展示边界；只用于抑制同一物理调用的 response fallback 重放。
    stream_observer_events: set[str] = field(default_factory=set)
    # 本次真实出站请求的未校准估算；响应返回后与 provider usage 成对，不对外展示。
    raw_context_estimate_tokens: int = 0
    # 稳定 provider 请求表面的摘要指纹；只用于拒绝跨模型/工具表面的错误校准复用。
    context_surface_fingerprint: str = ""


# LLM: 这是工具模型轮的统一生成入口；preflight compact、成本和完成追踪必须保持同一路径，变更要同步生成测试。
# 函数用途: 在上下文预检后调用模型，记录真实 native 工具使用和成本，再完成本轮响应归档。
def generate_model_response(request: ModelGenerateParams):
    request = _materialize_native_prompt_facts(request)
    if preflight := preflight_context_pressure_response(request):
        return preflight
    _trace_model_start(request)
    state = _start_model_generation(request)
    # 门槛5补证(维护记录): 返回与响应配对的 final_state——重试成功后
    # 用 attempt-2 的 state 收口(其 call_id 落 finished), 不落在 attempt-1。
    response, final_state = _generate_or_recover_context_pressure(request, state)
    _record_run_cost(request, response)  # 审计 #19/#2:真实 USD 成本累计到 owner/run 维度
    return _finish_model_generation(request, final_state, response)


# LLM: A typed native prompt's volatile suffix must enter the canonical IR before provider
# submission. The backend then receives an empty volatile adjunct, so every later tool request
# literally extends the earlier messages instead of moving changed facts behind newer results.
# 函数用途: 把本次原生请求的动态事实固化为追加式消息，并返回不再重复携带这些事实的 prompt。
def _materialize_native_prompt_facts(request: ModelGenerateParams) -> ModelGenerateParams:
    if not native_tool_use_active(request.params):
        return request
    layout = prompt_cache_layout(request.prompt)
    if layout is None:
        return request
    record_runtime_facts_turn_ir(request.params, layout.volatile_suffix)
    record_runtime_facts_turn_ir(
        request.params,
        conversation_runtime_state_section(request.params),
    )
    provider_prompt = CacheStructuredPrompt(
        layout.stable_prefix,
        "",
        stable_user_prefix=layout.stable_user_prefix,
        canonical_user_turn=layout.canonical_user_turn,
    )
    return replace(request, prompt=provider_prompt)


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
    if _has_ambiguous_active_turn_input(request):
        return None
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


# LLM: A timeout after provider admission has no reliable execution answer. When the same prompt
# carries active-turn input, a second physical call could deliver that user message twice.
# 函数用途: 判断当前模型请求是否带有已经越过或可能越过模型边界的补充消息。
def _has_ambiguous_active_turn_input(request: ModelGenerateParams) -> bool:
    state = getattr(request.params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    pending = state.get("_guidance_ack_ids")
    return bool(
        str(state.get("_guidance_submission_id") or "").strip()
        or (isinstance(pending, set) and pending)
    )


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


# LLM: 初始化必须让模型 token 经过 observed filter，同时把宿主原始 sink 单独保留给结构化运行事件。
# 函数用途: 为一次模型请求建立调用账本、流处理器、重连显示出口和 native 参数。
def _start_model_generation(request: ModelGenerateParams) -> _ModelGenerationState:
    _begin_model_turn_identity(request.params, request.tool_rounds)
    chunk_filter = _build_tool_boundary_chunk_filter(request)
    context_snapshot = model_visible_context_snapshot(
        request.agent,
        request.params,
        request.prompt,
    )
    ledger, call_id, first_token_estimate = start_model_call_record(
        request,
        context_snapshot=context_snapshot,
    )
    on_chunk = observed_chunk_filter(
        ledger=ledger,
        call_id=call_id,
        chunk_filter=chunk_filter,
        first_token_estimate=first_token_estimate,
        activity_callback=partial(
            _publish_runner_model_stream_activity,
            request,
            stream_kind="output",
        ),
    )
    tools = resolve_native_tools(request.agent, request.params)
    tool_choice = _model_turn_tool_choice(request.params, tools)
    _remember_model_turn_tool_choice(request.params, tool_choice)
    return _ModelGenerationState(
        agent=request.agent,
        params=request.params,
        chunk_filter=chunk_filter,
        ledger=ledger,
        call_id=call_id,
        tool_rounds=request.tool_rounds,
        first_token_timeout_seconds=first_token_estimate.timeout_seconds,
        on_chunk=on_chunk,
        retry_sink=request.params.effective_on_chunk,
        tools=tools,
        tool_choice=tool_choice,
        messages=_native_provider_messages(request.agent, request.params),
        system_instruction=provider_system_instruction(request.agent.backend),
        started_at=time.monotonic(),
        raw_context_estimate_tokens=context_snapshot.raw_estimated_tokens,
        context_surface_fingerprint=context_snapshot.context_surface_fingerprint,
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


# LLM: Semantic model deltas become a throttled no-content child-state heartbeat. The exact text
# continues only to the existing TUI sink/model ledger; this projection stores phase and counts.
# 函数用途: 在慢模型持续输出时定期刷新子代理状态，避免用户把长推理误认为卡死。
def _publish_runner_model_stream_activity(
    request: ModelGenerateParams,
    chunk: object,
    *,
    stream_kind: str,
) -> None:
    content_chars = len(str(chunk or ""))
    if content_chars <= 0:
        return
    params = request.params
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    try:
        from ..capability.runtime_config_reload import capability_config_for_agent

        config = capability_config_for_agent(request.agent)
        if not bool(
            getattr(config, "subagent_stream_activity_projection_enabled", True)
        ):
            return
        interval = max(
            1.0,
            float(getattr(config, "subagent_stream_activity_interval_seconds", 15)),
        )
    except Exception:
        interval = 15.0
    now = time.monotonic()
    turn_id = str(state.get("_current_model_turn_id") or "")
    previous = state.get(_RUNNER_STREAM_ACTIVITY_STATE_KEY)
    previous = previous if isinstance(previous, dict) else {}
    kind = str(stream_kind or "output").strip().lower()
    same_stream = (
        str(previous.get("turn_id") or "") == turn_id
        and str(previous.get("stream_kind") or "") == kind
    )
    try:
        last_emitted = float(previous.get("last_emitted_monotonic") or 0.0)
    except (TypeError, ValueError):
        last_emitted = 0.0
    try:
        previous_chars = max(0, int(previous.get("observed_chars") or 0))
    except (TypeError, ValueError):
        previous_chars = 0
    total_chars = (previous_chars if same_stream else 0) + content_chars
    state[_RUNNER_STREAM_ACTIVITY_STATE_KEY] = {
        "turn_id": turn_id,
        "stream_kind": kind,
        "last_emitted_monotonic": last_emitted,
        "observed_chars": total_chars,
    }
    if same_stream and now - last_emitted < interval:
        return
    state[_RUNNER_STREAM_ACTIVITY_STATE_KEY]["last_emitted_monotonic"] = now
    trace_runner_model_stream_active(
        RunnerModelStreamActivityTraceRequest(
            agent=request.agent,
            params=params,
            tool_rounds=request.tool_rounds,
            stream_kind=kind,
            observed_chars=total_chars,
        )
    )


def _native_provider_messages(agent: object, params: object) -> list[dict] | None:
    """native 下把 IR 历史和宿主运行时指引翻成厂商原生 messages。

    text 协议始终返回 ``None``。native 第一轮即使还没有 IR 工具历史，也必须
    返回 ``[]``，让 backend 保留“这是原生会话”的结构化事实，并从首个模型
    请求开始缓存稳定 prompt 与工具清单；不能把空 native 历史压成 text 路径。
    ``tool_context`` 有尚未转发的宿主指引时，继续返回结构化 user 消息。
    已结束会话在 provider_history_messages，本轮 user、工具往返和动态运行事实按时间顺序
    进入 IR；本函数只拼接这两段，不能重排消息或把 prompt 中的诊断副本再次发给 provider。
    """
    if not native_tool_use_active(params):
        return None
    history = getattr(params, "tool_ir_history", None)
    from ..backends.message_adapter import AnthropicMessageAdapter, strip_orphaned_tool_blocks

    seen = _forwarded_guidance_seen(params)
    guidance = unforwarded_runtime_guidance(
        getattr(params, "tool_context", None),
        seen,
    )
    if guidance:
        record_runtime_facts_turn_ir(params, "\n\n".join(guidance))
        history = getattr(params, "tool_ir_history", None)
    prior = [
        deepcopy(item)
        for item in list(getattr(params, "provider_history_messages", None) or [])
        if isinstance(item, dict)
    ]
    current = AnthropicMessageAdapter().to_provider_messages(history) if history else []
    messages = [*prior, *current]
    # Step 4 最后防线：发请求前再扫一遍孤儿（Step3 的整对回收漏了截断/异常中断/subagent
    # 提前结束/resume 等边界时，IR 仍可能残留「有 tool_use 无配对 tool_result」或反之）。
    # Anthropic 对孤儿一律 HTTP 400，这道 sweep 给孤儿 tool_use 补 stub、剔除孤儿
    # tool_result，保证出站永不带孤儿。
    messages = strip_orphaned_tool_blocks(messages)
    # 非工具运行时指引已经在翻译前写入 RuntimeFactsTurn；后续请求会永久带着它，
    # 不再出现“首轮发送、下一轮因 seen 去重反而消失”的瞬态消息。
    return messages


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


# LLM: Provider full-thinking must be published before the chunk filter flushes assistant text;
# otherwise a non-streaming thinking block appears after its answer and breaks transcript order.
# 函数用途: 先收口一次模型调用的显式思考，再刷新正文、账本与 trace。
def _finish_model_generation(request: ModelGenerateParams, state: _ModelGenerationState, response):
    response = _recover_unclosed_long_write_response(request, response)
    _publish_provider_thinking(request, state, response)
    state.chunk_filter.finish()
    record_model_call_finished(state.ledger, state.call_id, response)
    record_provider_context_observation(
        request.agent,
        request.params,
        raw_estimated_tokens=state.raw_context_estimate_tokens,
        context_surface_fingerprint=state.context_surface_fingerprint,
        response=response,
    )
    trace_runner_model_response_received(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            response=response,
        )
    )
    return response


# LLM: response 级完整思考只在流内 block-stop 尚未发布时兜底；仍只接受 type=thinking，禁止签名、redacted、tool_use 与普通 text。
# 函数用途: 为不支持流内思考终态的后端补发一次完整、默认折叠的模型思考。
def _publish_provider_thinking(
    request: ModelGenerateParams,
    state: _ModelGenerationState,
    response: object,
) -> None:
    if not _provider_thinking_projection_enabled(request.params):
        return
    if "thinking_completed" in getattr(state, "stream_observer_events", set()):
        return
    sink = getattr(request.params.effective_on_chunk, "write_thinking", None)
    if not callable(sink):
        return
    blocks = getattr(response, "assistant_content_blocks", None)
    if not isinstance(blocks, (list, tuple)):
        return
    parts = [
        str(block.get("thinking") or "").strip()
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    text = "\n\n".join(part for part in parts if part)
    if not text:
        return
    sink(
        text,
        duration_seconds=max(0.0, time.monotonic() - state.started_at),
    )


# LLM: Only backends declaring the typed thinking-completion capability may receive this keyword;
# untyped/fake/custom backends keep the historical generate call shape instead of failing at runtime.
# 函数用途: 只给明确支持思考流边界的后端组装增量/完成观察器，旧后端不接收陌生参数。
def _thinking_stream_observer(backend: object, state: object):
    params = getattr(state, "params", None)
    supports_typed_thinking = bool(
        getattr(backend, "supports_thinking_completion", False)
    )
    if not supports_typed_thinking or not _provider_thinking_projection_enabled(params):
        return None
    sink_owner = getattr(params, "effective_on_chunk", None)
    delta_sink = getattr(sink_owner, "write_thinking_delta", None)
    complete_sink = getattr(sink_owner, "write_thinking", None)
    activity_sink = partial(
        _publish_runner_model_stream_activity,
        ModelGenerateParams(
            agent=getattr(state, "agent", None),
            params=params,
            prompt="",
            tool_rounds=int(getattr(state, "tool_rounds", 0) or 0),
        ),
        stream_kind="thinking",
    )
    if callable(complete_sink):
        complete = partial(_publish_streamed_thinking_completion, state, complete_sink)
        return _ThinkingStreamObserver(delta_sink, complete, activity_sink)
    if callable(delta_sink):
        return _ThinkingStreamObserver(delta_sink, None, activity_sink)
    return None


# LLM: block-stop 的完整思考是本次物理调用的显示终态；标记必须先写，再调用 sink，避免重入路径重复 fallback。
# 函数用途: 在供应商思考块结束时立即封口 TUI 块，并记录本轮无需在 response 收尾再次发布。
def _publish_streamed_thinking_completion(state: object, sink: object, text: str) -> None:
    content = str(text or "")
    if not content:
        return
    events = getattr(state, "stream_observer_events", None)
    if isinstance(events, set):
        events.add("thinking_completed")
    sink(
        content,
        duration_seconds=max(
            0.0,
            time.monotonic() - float(getattr(state, "started_at", 0.0) or 0.0),
        ),
    )


# LLM: 该对象复用旧 on_thinking_delta 单参数接口并增加 typed complete 方法；不能缓存正文或参与模型历史。
# 类用途: 把思考增量与供应商块结束边界合成一个向后兼容的流观察器。
class _ThinkingStreamObserver:
    # LLM: 两个 sink 都来自同一宿主回合；complete sink 已绑定物理调用状态，不能跨调用复用。
    # 函数用途: 保存当前模型调用的思考增量和完成回调。
    def __init__(
        self,
        delta_sink: object,
        complete_sink: object,
        activity_sink: object = None,
    ) -> None:
        self._delta_sink = delta_sink
        self._complete_sink = complete_sink
        self._activity_sink = activity_sink

    # LLM: callable 形态保持旧 backend/collector 的 delta 合同；无 delta sink 时只静默等待 terminal。
    # 函数用途: 转发一段实时思考增量。
    def __call__(self, text: str) -> None:
        if callable(self._activity_sink):
            self._activity_sink(text)
        if callable(self._delta_sink):
            self._delta_sink(text)

    # LLM: 只有支持 provider block-stop 的 collector 会调用；不得在 response 收尾二次触发。
    # 函数用途: 转发当前完整思考块的结束事件。
    def complete(self, text: str) -> None:
        if callable(self._complete_sink):
            self._complete_sink(text)


# LLM: Only an actual task turn may project provider reasoning. The structured
# isolated scope marks a no-save presentation round whose private drafting must
# never appear as main-agent thinking or alter task state.
# 函数用途: 判断这次模型调用的思考是否属于用户正在观察的真实执行轮。
def _provider_thinking_projection_enabled(params: object) -> bool:
    return str(getattr(params, "context_scope", "") or "").strip().lower() != "isolated"


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
        idle_silence_seconds=_provider_timeout_idle_silence(record.ledger, record.call_id),
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
# LLM: This wrapper chooses direct or timeout-guard execution; the deeper shared backend boundary
# owns the durable pre-provider submission hook for every physical attempt.
# 函数用途: 按配置直接调用模型或启动超时守护线程，并统一等待结果。
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


# LLM: Streaming providers own request-local first-event/idle timing; non-stream providers alone may use the outer total guard.
# 函数用途: 按后端传输类型调用模型，并避免慢流通过修改共享 timeout 影响并发请求。
def _generate_backend_response(
    request: ModelGenerateParams, state: _ModelGenerationState, timeout: float
):
    backend = request.agent.backend
    # 流式传输通过 ProviderRequestOptions 获得 request-local 首包预算，并保留
    # backend.request_timeout 作为稳定的事件间 idle。共享实例不能被并发请求临时改写。
    if _transport_owns_stream_idle_timeout(request.agent):
        return _invoke_backend_generate(backend, request.prompt, state)
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


# LLM: backend 调用必须在 provider attempt observer 和全局并发槽内；observer 先落 canonical ledger 再投影 retry UI。
# 函数用途: 调用真实模型后端，并记录物理请求尝试、耗时、并发和费用。
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
        _trace_transport_retry(state, event)
        _publish_transport_retry(state.retry_sink, event)

    # 全局在飞 LLM 并发闸(T4 层4):默认关=nullcontext 零变化;配了 LLM_MAX_INFLIGHT 才封顶,
    # 拿槽在 llm_inflight 计数【之前】(槽满时等待期不算在飞,gauge 只反映真在飞)。
    with provider_attempt_observer(_observe_provider_attempt):
        with global_llm_admission_slot():
            llm_inflight(1)
            try:
                from .runtime.guidance import mark_injected_turn_input_submitted

                # This is the last local statement before backend.generate may
                # touch the network. Failure leaves provider I/O unstarted.
                mark_injected_turn_input_submitted(
                    state.agent,
                    state.params,
                    provider_call_id=state.call_id,
                )
                response = _do_backend_generate(backend, prompt, state)
            except Exception:
                record_llm_call(label, time.monotonic() - start, None, ok=False)
                raise
            finally:
                llm_inflight(-1)
    record_llm_call(label, time.monotonic() - start, response, ok=True)
    record_llm_cost(model, response)  # 真实 USD 成本按 model 累计(审计 #19 残余)
    return response


# LLM: transport observer 只按 retry_scheduled 等结构化字段投递显示事件；显示 sink 失败不得改变模型调用结果。
# 函数用途: 把 HTTP 层即将执行的退避同步给支持富记录的客户端。
def _publish_transport_retry(sink_owner: object, event: dict[str, object]) -> None:
    if event.get("status") != "failed" or event.get("retry_scheduled") is not True:
        return
    sink = getattr(sink_owner, "write_provider_retry", None)
    if not callable(sink):
        return
    try:
        sink(
            scope="transport",
            attempt=int(event.get("retry_attempt") or 1),
            total=int(event.get("retry_total") or 1),
            delay_seconds=float(event.get("retry_wait_seconds") or 0.0),
            error_type=str(event.get("error_type") or ""),
        )
    except Exception:
        return


# LLM: The durable retry projection reads only the transport observer's typed retry fields and
# cannot influence whether another request is attempted. Provider error bodies stay in the ledger.
# 函数用途: 在真正安排退避时更新当前子代理状态，后台任务也能看见正在第几次重连。
def _trace_transport_retry(
    state: _ModelGenerationState,
    event: dict[str, object],
) -> None:
    if event.get("status") != "failed" or event.get("retry_scheduled") is not True:
        return
    trace_runner_provider_retry_scheduled(
        RunnerProviderRetryTraceRequest(
            agent=state.agent,
            params=state.params,
            tool_rounds=state.tool_rounds,
            attempt=int(event.get("retry_attempt") or 1),
            total=int(event.get("retry_total") or 1),
            delay_seconds=float(event.get("retry_wait_seconds") or 0.0),
            error_type=str(event.get("error_type") or ""),
        )
    )


# LLM: 只有 backend 明确声明支持且当前确有原生工具 surface 时，才把工具参数 sink 传入；
# thinking block-stop 另用独立 capability，两个判断都不能靠 backend 名称或 prompt 文本。
# 函数用途: 选择本轮是否启用 provider 工具参数生成进度回调。
def _tool_input_progress_callback(backend: object, state: _ModelGenerationState):
    params = getattr(state, "params", None)
    sink = getattr(
        getattr(params, "effective_on_chunk", None),
        "write_tool_input_progress",
        None,
    )
    if (
        callable(sink)
        and bool(getattr(backend, "supports_tool_input_progress", False))
        and state.tools is not None
    ):
        return partial(_publish_tool_input_progress, state, sink)
    return None


# LLM: Tool-input progress forwards the provider's already-sanitized typed projection and emits
# the same throttled no-content runner activity used by text/reasoning streams.
# 函数用途: 同时更新工具参数生成状态并把原有进度对象交给 TUI sink。
def _publish_tool_input_progress(
    state: _ModelGenerationState,
    sink: object,
    payload: dict[str, object],
) -> None:
    request = ModelGenerateParams(
        agent=state.agent,
        params=state.params,
        prompt="",
        tool_rounds=state.tool_rounds,
    )
    _publish_runner_model_stream_activity(
        request,
        str(payload.get("received_chars") or ""),
        stream_kind="tool_input",
    )
    sink(payload)


# LLM: 仅把 backend 明确声明支持的 provider 增量/终态 callback 传给具备 typed sink
# 的宿主；fake/旧后端保持原关键字形态，流内思考终态不能在 response 收尾处重放。
# 函数用途: 按当前原生工具、思考和展示能力组装参数并调用一次模型后端。
def _do_backend_generate(backend, prompt: str, state: _ModelGenerationState):
    # text 协议(tools/messages 均为 None)保持原调用形态，不传新关键字，旁路/伪后端零改动。
    thinking_observer = _thinking_stream_observer(backend, state)
    tool_input_progress = _tool_input_progress_callback(backend, state)
    system_instruction = str(getattr(state, "system_instruction", "") or "")
    provider_options_supported = bool(
        getattr(backend, "supports_provider_request_options", False)
    )
    first_event_timeout = (
        max(0.0, float(state.first_token_timeout_seconds))
        if bool(getattr(backend, "stream_enabled", False))
        else None
    )
    _generate_started = time.monotonic()
    if state.tools is None and state.messages is None:
        kwargs: dict[str, object] = {"on_chunk": state.on_chunk}
        if provider_options_supported and (system_instruction or first_event_timeout is not None):
            kwargs["request_options"] = ProviderRequestOptions(
                system_instruction=system_instruction,
                first_event_timeout_seconds=first_event_timeout,
            )
        if thinking_observer is not None:
            kwargs["on_thinking_delta"] = thinking_observer
        result = backend.generate(prompt, **kwargs)
    else:
        kwargs: dict[str, object] = {"on_chunk": state.on_chunk}
        thinking_disabled = False
        if thinking_observer is not None:
            kwargs["on_thinking_delta"] = thinking_observer
        if tool_input_progress is not None:
            kwargs["on_tool_input_progress"] = tool_input_progress
        if state.tools is not None:
            kwargs["tools"] = state.tools
            tool_choice = state.tool_choice or ToolChoice.auto()
            kwargs["tool_choice"] = tool_choice
            thinking_disabled = tool_choice.mode != "auto"
        if provider_options_supported and (
            system_instruction or thinking_disabled or first_event_timeout is not None
        ):
            # LLM: 强制 tool_choice(specific/required/none)必须同时关思考——部分兼容端点
            # (如 工具运行时 zen)在思考模式下拒绝强制工具选择；typed options 避免继续扩张公开签名。
            kwargs["request_options"] = ProviderRequestOptions(
                system_instruction=system_instruction,
                thinking_disabled=thinking_disabled,
                first_event_timeout_seconds=first_event_timeout,
            )
        if state.messages is not None:
            kwargs["messages"] = state.messages
        result = backend.generate(prompt, **kwargs)
    if os.environ.get("MY_AGENT_STAGE_DEBUG") == "1":
        # 诊断行走 stderr（gateway 的 nohup 2>&1 已并入日志文件）。
        import sys

        print(
            "gateway run stage request_id=%s run_id=%s stage=model_generate"
            " elapsed_ms=%s"
            % (
                getattr(state.params, "request_id", "") or "",
                getattr(state.params, "run_id", "") or "",
                round((time.monotonic() - _generate_started) * 1000, 1),
            ),
            file=sys.stderr,
            flush=True,
        )
    return result


def _tool_write_inline_max_chars(request: ModelGenerateParams) -> int | None:
    return getattr(getattr(request.agent, "config", None), "tool_write_inline_max_chars", None)
