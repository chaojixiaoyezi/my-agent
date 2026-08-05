from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal

from ...backends import ModelResponse
from ...concurrency.interrupt import is_interrupted
from ...conversation.authority import conversation_transcript_is_authoritative
from ...memory_archive import estimate_tokens
from ...tooling.models import (
    ToolExecutionResult,
    ToolFailureStage,
    apply_tool_execution_facts,
)
from .._runtime_params import ToolLoopExecuteParams
from ..model.context_pressure import should_compact_before_more_tool_output
from ..runtime.context_compactor import runtime_compact_policy
from ..runtime.live_archive import archive_assistant_tool_round_if_enabled
from ..tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)
from .round_subagent_output import (
    SubagentOutputWriteCheck,
    is_subagent_output_json_write,
    subagent_output_json_response,
)

_STATEFUL_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "schedule_child_subagents",
}
_DEPENDENT_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "schedule_child_subagents",
    "send_guidance",
}
_CONTENT_OUTPUT_TOOLS = {
    "controlled_exec",
    "exec_command",
    "find_files",
    "list_files",
    "read_artifact",
    "read_file",
    "search_text",
    "shell",
    "shell_command",
    "watch_stream",
    "web_fetch",
    "web_search",
}


@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object


@dataclass(frozen=True)
class ToolRoundExecutionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    tool_rounds: int
    response: ModelResponse
    calls: list[dict[str, object]]
    execute_one: Callable[[ToolCallExecuteParams], ToolExecutionResult]
    record_one: Callable[[ToolCallRecordParams], None]
    current_prompt: str = ""


@dataclass(frozen=True)
class ToolProgressEvent:
    request: ToolRoundExecutionRequest
    idx: int
    payload: object
    phase: Literal["deferred", "started", "finished", "interrupted"]
    status: str
    started_at: float | None = None
    result: ToolExecutionResult | None = None


# LLM: 本入口顺序执行当前模型轮的 typed 工具调用；compact、安全中断、编排去重和记录顺序均是调用契约，变更要同步工具轮测试。
# 函数用途: 执行一轮模型请求的工具列表，逐项记录开始、结果、中断或因 compact 延后的真实状态。
def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    before_context_count = len(getattr(request.params, "tool_context", []) or [])
    _append_assistant_tool_round_context(request)
    calls = _calls_for_this_execution_round(request)
    subagent_output_written = False
    stateful_orchestration_seen = False
    handled_count = 0
    for idx, payload in enumerate(calls, start=1):
        payload = _bound_conversation_workspace_payload(request.agent, payload)
        tool_name = _tool_name(payload)
        # 协作中断安全点(批3):本线程被取消就不再开新工具,已完成的照常留痕。
        if is_interrupted():
            _record_interrupted_call(request, idx, payload)
            handled_count = idx
            break
        if _should_defer_for_compact(request, tool_name):
            _emit_tool_progress(ToolProgressEvent(request, idx, payload, "deferred", "延后"))
            result = _compact_deferred_result(tool_name)
            request.record_one(
                ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result)
            )
            _append_compact_deferred_notice(request, tool_name, idx)
            handled_count = idx
            break
        started_at = time.monotonic()
        _emit_tool_progress(ToolProgressEvent(request, idx, payload, "started", "开始"))
        if _should_defer_orchestration(stateful_orchestration_seen, tool_name):
            result = _deferred_orchestration_result(tool_name)
        else:
            result = request.execute_one(
                ToolCallExecuteParams(request.params, request.tool_rounds, idx, payload)
            )
        if result.duration_ms <= 0:
            apply_tool_execution_facts(
                result,
                duration_ms=(time.monotonic() - started_at) * 1000,
            )
        _emit_tool_progress(
            ToolProgressEvent(
                request,
                idx,
                payload,
                "finished",
                _finished_status(result),
                started_at,
                result,
            )
        )
        request.record_one(
            ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result)
        )
        handled_count = idx
        subagent_output_written = subagent_output_written or is_subagent_output_json_write(
            SubagentOutputWriteCheck(request.agent, request.params, payload, result)
        )
        stateful_orchestration_seen = (
            stateful_orchestration_seen or tool_name in _STATEFUL_ORCHESTRATION_TOOLS
        )
        if transition := _runtime_transition_after_tool(result):
            state = getattr(request.params, "live_archive_state", None)
            if isinstance(state, dict):
                state["pending_runtime_transition"] = {
                    **transition,
                    "tool": tool_name,
                    "tool_round": request.tool_rounds,
                    "tool_index": idx,
                }
            # The successful handler changed the durable authority used to
            # build model context.  Do not execute calls selected from the old
            # snapshot and do not ask the model to reason over that stale
            # snapshot again; completion.py ends this bounded slice and the
            # normal continuation reloads canonical state.
            break
        if _round_context_over_compact_budget(request, before_context_count):
            _record_remaining_content_calls_as_deferred(request, calls, start_idx=idx + 1)
            break
    _append_deferred_tool_call_notice(request, handled_count=handled_count)
    _enforce_turn_context_budget(request.params, before_context_count)
    return subagent_output_written


def _runtime_transition_after_tool(
    result: ToolExecutionResult,
) -> dict[str, str] | None:
    if not result.ok:
        return None
    envelope = result.result_envelope
    if not isinstance(envelope, dict):
        return None
    transition = envelope.get("runtime_transition")
    if not isinstance(transition, dict):
        return None
    kind = str(transition.get("kind") or "").strip()
    reason = str(transition.get("reason") or "").strip()
    resume = str(transition.get("resume") or "").strip()
    if kind != "context_refresh" or resume != "next_durable_slice" or not reason:
        return None
    return {"kind": kind, "reason": reason, "resume": resume}


def _bound_conversation_workspace_payload(agent: object, payload: object) -> object:
    """统一改写绑定前 prompt 遗留的占位目录，避免账本续上而产物另起目录。"""
    from ...conversation.task_promotion import rebase_bound_conversation_workspace_params

    return rebase_bound_conversation_workspace_params(agent, payload)


# 函数用途: 中断时给本工具留一条结构化"已中断"记录(进度+留痕一并处理)。
def _record_interrupted_call(request: ToolRoundExecutionRequest, idx: int, payload: object) -> None:
    _emit_tool_progress(ToolProgressEvent(request, idx, payload, "interrupted", "中断"))
    result = _interrupted_result(_tool_name(payload))
    request.record_one(
        ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result)
    )


# 函数用途: 中断时给本工具一条结构化"已中断"结果(模型可读懂并收尾)。
def _interrupted_result(tool_name: str) -> ToolExecutionResult:
    payload = json.dumps(
        {"error": "任务已被取消,本工具未执行。", "hint": "停止派发新动作,保存已有进展后收尾。"},
        ensure_ascii=False,
    )
    return ToolExecutionResult(
        tool_name,
        False,
        payload,
        error_code="CANCELLED",
        failure_stage=ToolFailureStage.RUNTIME_GATE.value,
    )


# LLM: 单回合聚合预算。
#   既有防线只管"单个结果过大就外置";本防线兜"单个都不大、本轮累计巨大"
#   (几十个中型 read/search 同轮返回)。超预算时从最大段开始截断到安全份额,
#   截口落在换行处,并注明恢复路径(重新调用工具/读档案)。纯框架层,模型无感。
_TURN_TOOL_CONTEXT_BUDGET_CHARS = 200_000
_TURN_BUDGET_KEEP_CHARS = 20_000


# 函数用途: 本轮工具输出总量超预算时,把最大的几段裁到安全大小(裁口带提示)。
def _enforce_turn_context_budget(params: ToolLoopExecuteParams, before_context_count: int) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or len(context) <= before_context_count:
        return
    indexed = list(enumerate(context))[before_context_count:]
    total = sum(len(str(text)) for _, text in indexed)
    for idx, text in sorted(indexed, key=lambda item: len(str(item[1])), reverse=True):
        if total <= _TURN_TOOL_CONTEXT_BUDGET_CHARS:
            return
        body = str(text)
        if len(body) <= _TURN_BUDGET_KEEP_CHARS:
            return
        context[idx] = _clip_at_newline(body, _TURN_BUDGET_KEEP_CHARS) + (
            "\n... [本轮工具输出总量超预算,此结果已截断;"
            "需要完整内容请用更窄的参数重新调用该工具,或按上方锚点读取档案。]"
        )
        total -= len(body) - len(context[idx])


# 函数用途: 把文本裁到限长,裁口尽量落在换行符上(避免半行残句)。
def _clip_at_newline(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", max_chars // 2, max_chars)
    return text[: cut if cut > 0 else max_chars]


# LLM: 只对会增加大量上下文的内容工具应用统一 compact 阈值；不得在此维护第二份百分比或 digest 状态。
# 函数用途: 判断当前内容工具是否应等会话先完成 compact 后再执行。
def _should_defer_for_compact(request: ToolRoundExecutionRequest, tool_name: str) -> bool:
    if tool_name not in _CONTENT_OUTPUT_TOOLS:
        return False
    if _conversation_owns_compaction(request.params):
        return False
    return should_compact_before_more_tool_output(
        request.agent,
        request.params,
        request.current_prompt,
    )


def _append_assistant_tool_round_context(request: ToolRoundExecutionRequest) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(request.response.text, request.calls)
    )
    request.params.tool_context.append(f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}")
    # 灰度双轨：native 下先为本轮开一条 AssistantTurn 并落定其可见文本（取该轮真实
    # ModelResponse.text）；同轮工具结果随后由 _record_tool_call 追加进这条 turn。
    _open_assistant_turn_ir_if_native(request)
    archive_assistant_tool_round_if_enabled(
        request.agent,
        request.params,
        tool_round=request.tool_rounds,
        response_text=request.response.text,
        tool_calls=request.calls,
    )


# LLM: 原生工具轮必须把 ModelResponse 的可见 text 与内部有序 content blocks 一起写入 IR；content blocks 不得进入 tool_context 展示文本。
# 函数用途: 在 native 模式下为当前模型轮建立完整的 assistant 历史，供下一轮模型请求续接。
def _open_assistant_turn_ir_if_native(request: ToolRoundExecutionRequest) -> None:
    from ..native_tool_protocol import native_tool_use_active
    from ..tool_ir_history import open_assistant_turn_ir

    if not native_tool_use_active(request.agent):
        return
    open_assistant_turn_ir(
        request.params,
        tool_rounds=request.tool_rounds,
        response_text=str(getattr(request.response, "text", "") or ""),
        response_content_blocks=list(
            getattr(request.response, "assistant_content_blocks", None) or []
        ),
    )


# LLM: 该内部记录必须明确“工具未执行”，供恢复轮和后续模型保持幂等；它不会直接投递给用户。
# 函数用途: 在工具上下文中登记因 compact 延后的调用，提醒恢复后从原目标继续。
def _append_compact_deferred_notice(
    request: ToolRoundExecutionRequest,
    tool_name: str,
    idx: int,
) -> None:
    request.params.tool_context.append(
        "[tool-system]\n"
        "当前上下文已达到 compact 阈值；"
        f"本轮第 {idx} 个 {tool_name or 'tool'} 调用已登记为 CONTEXT_COMPACT_DEFERRED，实际没有执行。\n"
        "系统会先走 compact/resume，再继续未执行的读取、搜索或命令；不要把这个工具调用当作已经完成。"
    )


def _compact_deferred_result(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name or "unknown",
        False,
        "CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行，恢复后从同一目标继续。",
        error_code="CONTEXT_COMPACT_DEFERRED",
        failure_stage=ToolFailureStage.RUNTIME_GATE.value,
    )


def _record_remaining_content_calls_as_deferred(
    request: ToolRoundExecutionRequest,
    calls: list[dict[str, object]],
    *,
    start_idx: int,
) -> None:
    deferred = 0
    for idx, payload in enumerate(calls[start_idx - 1 :], start=start_idx):
        tool_name = _tool_name(payload)
        if tool_name not in _CONTENT_OUTPUT_TOOLS:
            continue
        result = _compact_deferred_result(tool_name)
        request.record_one(
            ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result)
        )
        deferred += 1
    if deferred:
        request.params.tool_context.append(
            "[tool-system]\n"
            f"本轮剩余 {deferred} 个内容读取/检索工具已登记为 CONTEXT_COMPACT_DEFERRED；"
            "compact/resume 后系统会按这些结构化记录继续，不需要凭记忆重造调用。"
        )


def _calls_for_this_execution_round(request: ToolRoundExecutionRequest) -> list[dict[str, object]]:
    limit = _max_tool_calls_per_round(request)
    if limit <= 0 or len(request.calls) <= limit:
        return request.calls
    return request.calls[:limit]


def _max_tool_calls_per_round(request: ToolRoundExecutionRequest) -> int:
    value = _task_attribute_int(request, "max_tool_calls_per_round")
    if value is None:
        value = _agent_config_int(request.agent, "max_tool_calls_per_round")
    if value is None or value <= 0:
        return 0
    return value


def _task_attribute_int(request: ToolRoundExecutionRequest, key: str) -> int | None:
    attrs = getattr(request.params, "task_attributes", None)
    if not isinstance(attrs, dict) or key not in attrs:
        return None
    return _positiveish_int(attrs.get(key))


def _agent_config_int(agent: object, key: str) -> int | None:
    config = getattr(agent, "config", None)
    if config is None or not hasattr(config, key):
        return None
    return _positiveish_int(getattr(config, key))


def _positiveish_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append_deferred_tool_call_notice(
    request: ToolRoundExecutionRequest,
    *,
    handled_count: int,
) -> None:
    total = len(request.calls)
    if handled_count >= total:
        return
    deferred_count = total - handled_count
    request.params.tool_context.append(
        "[tool-system]\n"
        f"本轮模型请求了 {total} 个工具调用；为了避免单轮工具结果把上下文撑爆，"
        f"只处理到前 {handled_count} 个，剩余 {deferred_count} 个没有执行。\n"
        "如果某个工具被记录为 CONTEXT_COMPACT_DEFERRED，它只是可审计回执，不代表工具已经执行。\n"
        "下一轮请继续处理未完成的读取、写入或检查；不要把未执行的工具调用当作已经完成。"
    )


def _round_context_over_compact_budget(
    request: ToolRoundExecutionRequest, before_context_count: int
) -> bool:
    if _conversation_owns_compaction(request.params):
        return False
    if not str(request.current_prompt or ""):
        return False
    if not _persistent_compact_enabled(request.agent, request.params):
        return False
    policy = runtime_compact_policy(
        request.agent,
        save=True,
        context_scope=str(getattr(request.params, "context_scope", "default") or "default"),
    )
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    tool_context = list(getattr(request.params, "tool_context", []) or [])
    new_context = tool_context[before_context_count:]
    prompt_tokens = estimate_tokens(request.current_prompt) + estimate_tokens(new_context)
    return prompt_tokens >= threshold


def _conversation_owns_compaction(params: ToolLoopExecuteParams) -> bool:
    return bool(
        str(getattr(params, "context_scope", "") or "") == "conversation"
        and conversation_transcript_is_authoritative(params.task_attributes)
    )


def _persistent_compact_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


def _tool_name(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tool") or "").strip()


def _emit_tool_progress(event: ToolProgressEvent) -> None:
    on_chunk = getattr(event.request.params, "effective_on_chunk", None)
    if not callable(on_chunk):
        return
    tool_name = _tool_name(event.payload) or "unknown"
    detail = _payload_progress_detail(event.payload)
    elapsed = ""
    if event.started_at is not None:
        elapsed = f" {max(0.0, time.monotonic() - event.started_at):.2f}s"
    suffix = f": {detail}" if detail else ""
    legacy_text = (
        f"\n[工具] round={event.request.tool_rounds} "
        f"#{event.idx} {tool_name} {event.status}{elapsed}{suffix}\n"
    )
    progress_writer = getattr(on_chunk, "write_progress", None)
    if callable(progress_writer):
        progress_writer(_structured_tool_progress(event, tool_name, detail), legacy_text)
        return
    try:
        on_chunk(legacy_text)
    except Exception:
        return


def _structured_tool_progress(
    event: ToolProgressEvent,
    tool_name: str,
    detail: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "round": event.request.tool_rounds,
        "call_index": event.idx,
        "tool": tool_name,
        "phase": event.phase,
        "status": event.status,
    }
    if detail:
        payload["detail"] = _public_progress_text(event, detail, max_chars=240)
    if event.result is not None:
        payload["ok"] = bool(event.result.ok)
        payload["handler_executed"] = bool(event.result.handler_executed)
        payload["duration_ms"] = max(0, int(event.result.duration_ms or 0))
        if event.result.failure_stage:
            payload["failure_stage"] = event.result.failure_stage
        if event.result.error_code:
            payload["error_code"] = event.result.error_code
        output = _public_progress_text(event, event.result.output, max_chars=1600)
        if output:
            payload["output"] = output
    if event.started_at is not None:
        payload["elapsed_seconds"] = round(
            max(0.0, time.monotonic() - event.started_at),
            3,
        )
    return payload


def _public_progress_text(
    event: ToolProgressEvent,
    value: object,
    *,
    max_chars: int,
) -> str:
    from ...conversation.channels import INTERNAL_SIGNAL_PREFIXES, project_user_reply
    from ...tooling.mcp_client import sanitize_credentials

    text = sanitize_credentials(str(value or ""))
    if any(marker in text for marker in INTERNAL_SIGNAL_PREFIXES):
        return "（内部运行状态已省略）"
    owner_home = str(
        getattr(getattr(event.request.agent, "home_paths", None), "owner_home_dir", "") or ""
    )
    if owner_home:
        text = text.replace(owner_home, "~/.my-agent/owner")
    text = project_user_reply(text).content
    if len(text) <= max_chars:
        return text
    keep_head = max_chars * 2 // 3
    keep_tail = max_chars - keep_head
    return f"{text[:keep_head]}\n…（内容过长，已省略）…\n{text[-keep_tail:]}"


def _finished_status(result: ToolExecutionResult) -> str:
    return "完成" if result.ok else f"失败({result.error_code or 'ERROR'})"


def _payload_progress_detail(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("path", "artifact_ref", "root_id", "run_id", "status", "scope"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _shorten(value)
    command = str(payload.get("command") or "").strip()
    if command:
        return _shorten(command)
    items = payload.get("items")
    if isinstance(items, list):
        return f"items={len(items)}"
    return ""


def _shorten(value: str, limit: int = 100) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _deferred_orchestration_result(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name or "unknown",
        False,
        "同一轮已经执行过会创建或改变子代理树的工具调用，"
        "后续编排工具已延后。请先读取上一条工具的真实输出，"
        "下一轮再使用返回的 created_run_ids/actionable_run_ids 调用 dispatch_subagents。",
        error_code="ORCHESTRATION_CALL_DEFERRED",
        failure_stage=ToolFailureStage.RUNTIME_GATE.value,
    )


def _should_defer_orchestration(stateful_orchestration_seen: bool, tool_name: str) -> bool:
    return (
        stateful_orchestration_seen
        and tool_name in _DEPENDENT_ORCHESTRATION_TOOLS
        and tool_name != "create_subagents"
    )
