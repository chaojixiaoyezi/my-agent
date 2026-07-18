
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..tooling.models import ToolExecutionResult
from ..tooling.write_boundary import declared_write_paths
from .audit_dispatch import audit_privileged_tool_call
from .parameters import (
    _one_shot_tool_call_is_duplicate,
    _one_shot_tool_call_keys,
)
from .runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .subagent.attempt_guard import stale_subagent_attempt_result
from .tool_guard.agent_budget_stage import (
    ToolAgentBudgetStageRequest,
    maybe_block_tool_agent_budget,
)
from .tool_loop.recovery import tool_payload_with_run_scope
from .tool_loop.round_execution import ToolCallExecuteParams
from .tool_runtime_ledger import write_boundary_with_runtime_ledger


@dataclass(frozen=True)
class ToolCallRuntimeRequest:
    agent: object
    request: ToolCallExecuteParams
    payload: dict[str, object]
    trace_request: RunnerToolStageTraceRequest


def guarded_tool_call_result(runtime_request: ToolCallRuntimeRequest):
    request = runtime_request.request
    payload = runtime_request.payload
    trace_request = runtime_request.trace_request
    if _one_shot_tool_call_is_duplicate(payload, request.params.one_shot_tool_calls):
        result = _duplicate_one_shot_result(payload)
        return _trace_finished_result(trace_request, result)
    stale_result = stale_subagent_attempt_result(runtime_request.agent, payload)
    if stale_result is not None:
        return _trace_finished_result(trace_request, stale_result)
    return maybe_block_tool_agent_budget(ToolAgentBudgetStageRequest(runtime_request.agent, request, payload))


def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    promotion_error = _promote_conversation_task_for_work_tool(runtime_request)
    if promotion_error is not None:
        return _trace_finished_result(runtime_request.trace_request, promotion_error)
    one_shot_keys = _one_shot_tool_call_keys(runtime_request.payload)
    executable_payload = tool_payload_with_run_scope(
        runtime_request.agent,
        runtime_request.request.params,
        runtime_request.payload,
        call_id=_runtime_tool_call_id(runtime_request),
    )
    result = runtime_request.agent.tools.execute_call(
        executable_payload,
        allowed_tools=runtime_request.request.params.allowed_tools,
        granted_capabilities=runtime_request.request.params.granted_capabilities,
        write_boundary=write_boundary_with_runtime_ledger(runtime_request.agent, runtime_request.request.params),
    )
    audit_privileged_tool_call(runtime_request.agent, executable_payload, result)  # 特权动作落审计(审计 #13)
    if one_shot_keys and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.update(one_shot_keys)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return result


def _promote_conversation_task_for_work_tool(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolExecutionResult | None:
    """在工具网关唯一执行缝隙按 ToolSpec 晋升，普通聊天入站本身不创建任务目录。"""
    tool_name = str(runtime_request.payload.get("tool") or "").strip()
    tools = getattr(getattr(runtime_request.agent, "tools", None), "tools", {})
    tool = tools.get(tool_name) if isinstance(tools, dict) else None
    spec = getattr(tool, "spec", None)
    if getattr(spec, "promotes_task", False) is not True:
        return None
    from ..tooling._persona_write_guard import _persona_runtime_redirect_error

    persona_error = _persona_runtime_redirect_error(runtime_request.agent, runtime_request.payload)
    if persona_error:
        return ToolExecutionResult(
            tool_name,
            False,
            persona_error,
            error_code="PERSONA_WRITE_REQUIRES_TOOL",
        )
    mutation_selection_error = _select_exact_mutation_workspace(runtime_request)
    if mutation_selection_error is not None:
        return mutation_selection_error
    current = getattr(runtime_request.agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    conversation_thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    from ..conversation.task_promotion import (
        conversation_workspace_decision,
        promote_current_conversation_task,
    )

    decision = conversation_workspace_decision(runtime_request.agent)
    if decision is not None:
        return ToolExecutionResult(
            tool_name or "conversation_task_binding",
            False,
            json.dumps(decision, ensure_ascii=False),
            error_code="CONVERSATION_WORKSPACE_DECISION_REQUIRED",
        )

    promoted = promote_current_conversation_task(runtime_request.agent)
    if promoted is not None or not conversation_thread_id:
        return None
    return ToolExecutionResult(
        tool_name or "conversation_task_binding",
        False,
        "CONVERSATION_TASK_BINDING_FAILED: 当前执行请求无法可靠绑定到持久任务，已阻止本次工作步骤。",
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )


def _select_exact_mutation_workspace(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolExecutionResult | None:
    """Rebind an exact old task selected by a structured filesystem mutation path.

    会话运行时 keeps one working directory across turns.  my-agent additionally isolates
    owner task directories, so an absolute write target inside one exact task is the
    machine-readable equivalent of selecting that workspace.  Reads never select,
    relative paths never guess, and paths spanning multiple tasks remain denied by
    the normal write boundary.
    """

    agent = runtime_request.agent
    payload = runtime_request.payload
    tool_name = str(payload.get("tool") or "").strip()
    raw_targets = declared_write_paths(tool_name, payload)
    targets = _absolute_mutation_targets(raw_targets)
    if not targets or len(targets) != len(raw_targets):
        return None
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return None
    try:
        links, load_errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if load_errors:
        return None
    from ..conversation.task_promotion import (
        conversation_task_selection_blocker,
        is_user_selectable_conversation_task,
        select_current_conversation_task,
    )

    candidates = [
        link
        for link in links
        if is_user_selectable_conversation_task(link)
        and _all_targets_inside_task(targets, getattr(link, "task_path", ""))
    ]
    if len(candidates) != 1:
        return None
    selected_id = str(getattr(candidates[0], "task_id", "") or "").strip()
    if not selected_id or str(attrs.get("conversation_task_id") or "").strip() == selected_id:
        return None
    blocker = conversation_task_selection_blocker(agent, selected_id)
    if blocker is not None:
        return ToolExecutionResult(
            tool_name or "conversation_task_binding",
            False,
            json.dumps(
                {
                    "ok": False,
                    "error": "The exact target task cannot be selected while its execution state is busy or unavailable.",
                    "task_id": selected_id,
                    **blocker,
                },
                ensure_ascii=False,
            ),
            error_code=(
                "CONVERSATION_TASK_ALREADY_RUNNING"
                if blocker.get("state_available") is True
                else "CONVERSATION_TASK_STATE_UNAVAILABLE"
            ),
        )
    selected = select_current_conversation_task(agent, selected_id)
    if selected is not None:
        return None
    return ToolExecutionResult(
        tool_name or "conversation_task_binding",
        False,
        json.dumps(
            {
                "ok": False,
                "error": "The exact target task workspace could not be selected safely.",
                "task_id": selected_id,
            },
            ensure_ascii=False,
        ),
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )


def _absolute_mutation_targets(raw_targets: list[str]) -> list[Path]:
    targets: list[Path] = []
    for raw in raw_targets:
        try:
            candidate = Path(str(raw)).expanduser()
            if not candidate.is_absolute():
                continue
            targets.append(candidate.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return targets


def _all_targets_inside_task(targets: list[Path], raw_task_root: object) -> bool:
    text = str(raw_task_root or "").strip()
    if not text:
        return False
    try:
        task_root = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    if not task_root.exists():
        return False
    return all(_path_is_relative_to(target, task_root) for target in targets)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _runtime_tool_call_id(runtime_request: ToolCallRuntimeRequest) -> str:
    return f"round-{runtime_request.trace_request.tool_rounds}-tool-{runtime_request.trace_request.idx}"


def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolExecutionResult:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolExecutionResult(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
    )


def _trace_finished_result(
    trace_request: RunnerToolStageTraceRequest,
    result: ToolExecutionResult,
):
    trace_runner_tool_call_finished(_finished_trace_request_from_trace(trace_request, result))
    return result


def _finished_trace_request(runtime_request: ToolCallRuntimeRequest, result: ToolExecutionResult):
    return _finished_trace_request_from_trace(runtime_request.trace_request, result)


def _finished_trace_request_from_trace(trace_request: RunnerToolStageTraceRequest, result: ToolExecutionResult):
    return RunnerToolStageTraceRequest(
        agent=trace_request.agent,
        params=trace_request.params,
        tool_rounds=trace_request.tool_rounds,
        idx=trace_request.idx,
        payload=trace_request.payload,
        result=result,
    )


def _one_shot_result_consumes_key(result: ToolExecutionResult) -> bool:
    if not result.ok:
        return False
    return not _orchestration_result_is_blocked(result.output)


def _orchestration_result_is_blocked(output: object) -> bool:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("blocked"))
