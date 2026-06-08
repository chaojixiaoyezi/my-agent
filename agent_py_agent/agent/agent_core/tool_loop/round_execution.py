
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from ...memory_archive import estimate_tokens
from ...tooling.models import ToolExecutionResult
from .._runtime_params import ToolLoopExecuteParams
from ..model.context_pressure import (
    mark_tool_context_digest_pending,
    should_compact_before_more_tool_output,
)
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
    "web_fetch",
    "web_search",
}
_CHECKPOINT_TOOLS = {
    "submit_for_acceptance",
    "task_progress",
    "write_file",
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
    status: str
    started_at: float | None = None


def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    before_context_count = len(getattr(request.params, "tool_context", []) or [])
    _append_assistant_tool_round_context(request)
    calls = _calls_for_this_execution_round(request)
    subagent_output_written = False
    stateful_orchestration_seen = False
    handled_count = 0
    read_since_checkpoint: list[dict[str, object]] = []
    for idx, payload in enumerate(calls, start=1):
        tool_name = _tool_name(payload)
        if _should_defer_for_compact_digest(request, tool_name):
            _emit_tool_progress(ToolProgressEvent(request, idx, payload, "延后"))
            result = _compact_deferred_result(tool_name)
            request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
            _append_compact_digest_deferred_notice(request, tool_name, idx)
            handled_count = idx
            break
        started_at = time.monotonic()
        _emit_tool_progress(ToolProgressEvent(request, idx, payload, "开始"))
        if stateful_orchestration_seen and tool_name in _DEPENDENT_ORCHESTRATION_TOOLS:
            result = _deferred_orchestration_result(tool_name)
        else:
            result = request.execute_one(
                ToolCallExecuteParams(request.params, request.tool_rounds, idx, payload)
            )
        _emit_tool_progress(ToolProgressEvent(request, idx, payload, _finished_status(result), started_at))
        request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
        if result.ok and tool_name == "read_file":
            read_since_checkpoint.append(dict(payload) if isinstance(payload, dict) else {})
        if result.ok and tool_name in _CHECKPOINT_TOOLS:
            read_since_checkpoint.clear()
        mark_tool_context_digest_pending(request.params)
        handled_count = idx
        subagent_output_written = subagent_output_written or is_subagent_output_json_write(
            SubagentOutputWriteCheck(request.agent, request.params, payload, result)
        )
        stateful_orchestration_seen = (
            stateful_orchestration_seen or tool_name in _STATEFUL_ORCHESTRATION_TOOLS
        )
        if _round_context_over_compact_budget(request, before_context_count):
            _record_remaining_content_calls_as_deferred(request, calls, start_idx=idx + 1)
            break
    _append_long_read_fact_reminder(request, read_since_checkpoint)
    _append_deferred_tool_call_notice(request, handled_count=handled_count)
    return subagent_output_written


def _should_defer_for_compact_digest(request: ToolRoundExecutionRequest, tool_name: str) -> bool:
    if tool_name not in _CONTENT_OUTPUT_TOOLS:
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
    request.params.tool_context.append(
        f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}"
    )
    archive_assistant_tool_round_if_enabled(
        request.agent,
        request.params,
        tool_round=request.tool_rounds,
        response_text=request.response.text,
        tool_calls=request.calls,
    )


def _append_compact_digest_deferred_notice(
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
    )


def _record_remaining_content_calls_as_deferred(
    request: ToolRoundExecutionRequest,
    calls: list[dict[str, object]],
    *,
    start_idx: int,
) -> None:
    deferred = 0
    for idx, payload in enumerate(calls[start_idx - 1:], start=start_idx):
        tool_name = _tool_name(payload)
        if tool_name not in _CONTENT_OUTPUT_TOOLS:
            continue
        result = _compact_deferred_result(tool_name)
        request.record_one(ToolCallRecordParams(request.params, request.tool_rounds, idx, payload, result))
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


def _append_long_read_fact_reminder(
    request: ToolRoundExecutionRequest,
    read_since_checkpoint: list[dict[str, object]],
) -> None:
    chunked_reads = [item for item in read_since_checkpoint if _chunked_read_file_call(item)]
    if not chunked_reads:
        return
    recent = ", ".join(_read_call_pointer(item) for item in chunked_reads[-3:])
    request.params.tool_context.append(
        "[tool-system:long-read-facts]\n"
        "刚才已经读取了一段或多段正文，但这一轮还没有看到新的 task_progress/write_file 检查点。\n"
        "如果这些正文里有最终报告需要逐项保留的事实，请下一轮先把对象、事实和 source/offset/行号证据写入 task_progress 或当前任务 work 草稿，"
        "再继续读取下一段；不要只写“已覆盖某个范围”来代替逐项事实。\n"
        f"recent_reads: {recent}"
    )


def _chunked_read_file_call(payload: dict[str, object]) -> bool:
    if _tool_name(payload) != "read_file":
        return False
    return any(payload.get(key) not in (None, "") for key in ("offset", "max_chars", "start_line", "end_line"))


def _read_call_pointer(payload: dict[str, object]) -> str:
    path = str(payload.get("path") or "").strip()
    if not path:
        path = "<unknown>"
    offset = payload.get("offset")
    start_line = payload.get("start_line")
    end_line = payload.get("end_line")
    if offset not in (None, ""):
        return f"{path}@offset={offset}"
    if start_line not in (None, "") or end_line not in (None, ""):
        return f"{path}@lines={start_line or '?'}-{end_line or '?'}"
    return path


def _round_context_over_compact_budget(request: ToolRoundExecutionRequest, before_context_count: int) -> bool:
    if not str(request.current_prompt or ""):
        return False
    policy = runtime_compact_policy(request.agent, save=True)
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    tool_context = list(getattr(request.params, "tool_context", []) or [])
    new_context = tool_context[before_context_count:]
    prompt_tokens = estimate_tokens(request.current_prompt) + estimate_tokens(new_context)
    return prompt_tokens >= threshold


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
    try:
        on_chunk(
            f"\n[工具] round={event.request.tool_rounds} "
            f"#{event.idx} {tool_name} {event.status}{elapsed}{suffix}\n"
        )
    except Exception:
        return


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
    return f"{text[:limit - 3]}..."


def _deferred_orchestration_result(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name or "unknown",
        False,
        "同一轮已经执行过会创建或改变子代理树的工具调用，"
        "后续编排工具已延后。请先读取上一条工具的真实输出，"
        "下一轮再使用返回的 created_run_ids/actionable_run_ids 调用 dispatch_subagents。",
    )
