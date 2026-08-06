
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from ..tooling.models import (
    ToolFailureStage,
    ToolHandlerOutcome,
    apply_tool_execution_facts,
)
from ..tooling.runtime_contracts import ToolCall, ToolResult
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
from .tool_loop.round_execution import ToolCallExecuteParams
from .tool_runtime_ledger import write_boundary_with_runtime_ledger


@dataclass(frozen=True)
class ToolCallRuntimeRequest:
    agent: object
    request: ToolCallExecuteParams
    call: ToolCall

    @property
    def trace_request(self) -> RunnerToolStageTraceRequest:
        return RunnerToolStageTraceRequest(
            agent=self.agent,
            params=self.request.params,
            tool_rounds=self.request.tool_rounds,
            idx=self.request.idx,
            call=self.call,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "tool": self.call.tool_name,
            "call_id": self.call.call_id,
            **self.call.arguments,
        }


def guarded_tool_call_result(runtime_request: ToolCallRuntimeRequest):
    # Runtime guards reject before Registry handlers; keep that boundary explicit for recovery and audit.
    request = runtime_request.request
    payload = runtime_request.payload
    worker_scope = _audit_source_worker_tool_scope_result(
        runtime_request.agent,
        payload,
    )
    if worker_scope is not None:
        apply_tool_execution_facts(
            worker_scope,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return worker_scope
    if _one_shot_tool_call_is_duplicate(payload, request.params.one_shot_tool_calls):
        result = apply_tool_execution_facts(
            _duplicate_one_shot_result(payload),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return result
    stale_result = stale_subagent_attempt_result(runtime_request.agent, payload)
    if stale_result is not None:
        apply_tool_execution_facts(
            stale_result,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return stale_result
    return maybe_block_tool_agent_budget(ToolAgentBudgetStageRequest(runtime_request.agent, request, payload))


def _audit_source_worker_tool_scope_result(
    agent: object,
    payload: object,
) -> ToolHandlerOutcome | None:
    """Recheck a source worker's least-privilege scope at the final tool seam."""
    if not isinstance(payload, dict):
        return None
    from ..common.audit_activation import (
        audit_worker_tool_scope,
        current_audit_attributes,
    )

    attrs = current_audit_attributes(agent)
    tool_name = str(payload.get("tool") or "").strip()
    allowed_tools = audit_worker_tool_scope(attrs)
    if not allowed_tools or tool_name in allowed_tools:
        return None
    return ToolHandlerOutcome(
        tool_name,
        False,
        "Audit 来源工作者只能使用当前结构化阶段的最小工具集。",
        error_code="TOOL_NOT_ALLOWED",
    )


# LLM: 最终 Tool Gateway 调用必须携带模型看到的同一 run 快照，不能在执行时重新扩大工具宇宙。
# 函数用途: 执行并审计一个已追踪工具调用，同时维护任务晋升、幂等记录和被动验收事实。
def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    from .tool_call_archive_record import archive_tool_output_projection
    from .tool_loop.recovery import runtime_run_scope

    one_shot_keys = _one_shot_tool_call_keys(runtime_request.payload)
    execution = runtime_request.agent.tools.execute_tool(
        runtime_request.call,
        write_boundary=write_boundary_with_runtime_ledger(runtime_request.agent, runtime_request.request.params),
        runtime_snapshot=runtime_request.request.params.tool_runtime_snapshot,
        trusted_run_context={
            "task_attributes": dict(
                runtime_request.request.params.task_attributes
                if isinstance(runtime_request.request.params.task_attributes, dict)
                else {}
            ),
            "run_scope": runtime_run_scope(
                runtime_request.agent,
                runtime_request.request.params,
            ).to_dict(),
        },
        cancellation_token=getattr(runtime_request.request.params, "cancellation_token", None),
        required_action=_required_action_for_call(runtime_request),
        pre_handler_gate=lambda call: _pre_handler_gate(
            replace(runtime_request, call=call)
        ),
        output_archiver=lambda call, outcome: archive_tool_output_projection(
            runtime_request.agent,
            runtime_request.request.params,
            call,
            outcome,
        ),
    )
    result = execution.result
    executable_payload = {
        "tool": execution.call.tool_name,
        "call_id": execution.call.call_id,
        **execution.call.arguments,
    }
    result = _record_passive_verification(runtime_request.agent, execution.call, result)
    execution = replace(execution, result=result)
    audit_privileged_tool_call(runtime_request.agent, executable_payload, result)  # 特权动作落审计(审计 #13)
    if one_shot_keys and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.update(one_shot_keys)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return execution


def _pre_handler_gate(runtime_request: ToolCallRuntimeRequest) -> ToolHandlerOutcome | None:
    guard_result = guarded_tool_call_result(runtime_request)
    if guard_result is not None:
        return apply_tool_execution_facts(
            guard_result,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    promotion_error = _promote_conversation_task_for_work_tool(runtime_request)
    if promotion_error is None:
        return None
    return apply_tool_execution_facts(
        promotion_error,
        failure_stage=ToolFailureStage.RUNTIME_GATE,
        handler_executed=False,
    )


def _required_action_for_call(runtime_request: ToolCallRuntimeRequest) -> object | None:
    action_id = runtime_request.call.required_action_id
    snapshot = getattr(runtime_request.request.params, "effective_contract_snapshot", None)
    for action in tuple(getattr(snapshot, "required_actions", ()) or ()):
        if str(getattr(action, "action_id", "") or "") == action_id:
            return action
    return None


def _record_passive_verification(
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    """Record advisory verification facts at the one shared tool seam.

    The local import keeps the generic tool runtime independent from the
    owner-scoped persistence package during module initialization.
    """

    from ..verification.runtime import record_tool_verification

    return record_tool_verification(agent, call, result)


def _promote_conversation_task_for_work_tool(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolHandlerOutcome | None:
    """在工具网关唯一执行缝隙按 ToolRuntimePolicy 晋升任务。"""
    tool_name = str(runtime_request.payload.get("tool") or "").strip()
    snapshot = runtime_request.request.params.tool_runtime_snapshot
    runtime = snapshot.runtime(tool_name) if snapshot is not None else None
    if runtime is None or runtime.runtime_policy.promotes_task is not True:
        return None
    from ..tooling._persona_write_guard import _persona_runtime_redirect_error

    handler_root = getattr(runtime.handler, "workspace_root", None)
    persona_error = _persona_runtime_redirect_error(
        runtime_request.agent,
        runtime_request.payload,
        workspace_root=(
            Path(handler_root).expanduser().resolve(strict=False)
            if handler_root
            else None
        ),
    )
    if persona_error:
        return ToolHandlerOutcome(
            tool_name,
            False,
            persona_error,
            error_code="PERSONA_WRITE_REQUIRES_TOOL",
        )
    mutation_binding_error = _bind_declared_mutation_workspace(runtime_request)
    if mutation_binding_error is not None:
        return mutation_binding_error
    current = getattr(runtime_request.agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    conversation_thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    from ..conversation.task_promotion import (
        conversation_workspace_execution_blocker,
        promote_current_conversation_task,
    )

    decision = conversation_workspace_execution_blocker(runtime_request.agent)
    if decision is not None:
        return ToolHandlerOutcome(
            tool_name or "conversation_task_binding",
            False,
            json.dumps(decision, ensure_ascii=False),
            error_code=(
                "CONVERSATION_TASK_ALREADY_RUNNING"
                if decision.get("state_available") is True
                else "CONVERSATION_TASK_STATE_UNAVAILABLE"
            ),
        )

    promoted = promote_current_conversation_task(runtime_request.agent)
    if promoted is not None or not conversation_thread_id:
        return None
    return ToolHandlerOutcome(
        tool_name or "conversation_task_binding",
        False,
        "CONVERSATION_TASK_BINDING_FAILED: 当前执行请求无法可靠绑定到持久任务，已阻止本次工作步骤。",
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )


# LLM: Workspace binding accepts only an exact absolute target or canonical owner-relative
# tasks/... target; both must resolve to one reusable workspace before any write gate is changed.
# 函数用途: 根据结构化写入路径恢复同一会话的原任务目录；普通相对路径和歧义路径仍保持拒绝。
def _bind_declared_mutation_workspace(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolHandlerOutcome | None:
    """Rebind an exact old workspace named by a structured filesystem mutation path.

    会话运行时 keeps one working directory across turns.  my-agent additionally isolates
    owner task directories, so an absolute write target inside one exact task is the
    machine-readable equivalent of selecting that workspace.  The canonical
    owner-relative ``tasks/...`` form is equally unambiguous and is resolved only
    against the current thread's durable owner home.  Other relative paths never
    guess, reads never select, and paths spanning multiple tasks remain denied by
    the normal write boundary.
    """

    agent = runtime_request.agent
    payload = runtime_request.payload
    tool_name = str(payload.get("tool") or "").strip()
    raw_targets = declared_write_paths(tool_name, payload)
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
        thread = store.load_thread(thread_id)
        links, load_errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if thread is None or load_errors:
        return None
    targets = _canonical_mutation_targets(
        raw_targets,
        owner_home=getattr(thread, "owner_home", ""),
    )
    if not targets or len(targets) != len(raw_targets):
        return None
    selected_link = _unique_exact_mutation_workspace(links, targets)
    if selected_link is None:
        return None
    return _bind_exact_mutation_workspace(agent, tool_name, attrs, selected_link)


def _unique_exact_mutation_workspace(links: list[object], targets: list[Path]):
    from ..conversation.task_promotion import is_reusable_conversation_workspace

    candidates = [
        link
        for link in links
        if is_reusable_conversation_workspace(link)
        and _all_targets_inside_task(targets, getattr(link, "task_path", ""))
    ]
    return candidates[0] if len(candidates) == 1 else None


def _bind_exact_mutation_workspace(
    agent: object,
    tool_name: str,
    attrs: dict[str, object],
    selected_link: object,
) -> ToolHandlerOutcome | None:
    from ..conversation.task_promotion import (
        bind_current_conversation_workspace,
        conversation_task_execution_blocker,
        rebase_subagent_conversation_workspace,
    )
    from .runner.context import current_subagent_run_id

    selected_id = str(getattr(selected_link, "task_id", "") or "").strip()
    current_task_id = str(attrs.get("conversation_task_id") or "").strip()
    is_subagent = bool(current_subagent_run_id(agent))
    if not selected_id or (not is_subagent and current_task_id == selected_id):
        return None
    # A child targeting its own parent task is one of that task's executors, so the
    # parent's live claim must not block its local cwd.  A different live task still
    # blocks the child to prevent two unrelated task trees writing the same workspace.
    blocker = (
        None
        if is_subagent and current_task_id == selected_id
        else conversation_task_execution_blocker(agent, selected_id)
    )
    if blocker is not None:
        return _mutation_workspace_blocked_result(tool_name, selected_id, blocker)
    if is_subagent:
        if rebase_subagent_conversation_workspace(agent, selected_link):
            return None
        return _mutation_workspace_binding_failed(
            tool_name,
            selected_id,
            "The child runner could not bind the exact target workspace safely.",
        )
    selected = bind_current_conversation_workspace(agent, selected_id)
    if selected is not None:
        return None
    return _mutation_workspace_binding_failed(
        tool_name,
        selected_id,
        "The exact target task workspace could not be bound safely.",
    )


def _mutation_workspace_blocked_result(
    tool_name: str,
    selected_id: str,
    blocker: dict[str, object],
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        tool_name or "conversation_task_binding",
        False,
        json.dumps(
            {
                "ok": False,
                "error": "The exact target workspace cannot be bound while its execution state is busy or unavailable.",
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


def _mutation_workspace_binding_failed(
    tool_name: str,
    selected_id: str,
    error: str,
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        tool_name or "conversation_task_binding",
        False,
        json.dumps(
            {"ok": False, "error": error, "task_id": selected_id},
            ensure_ascii=False,
        ),
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )


# LLM: Owner-relative resolution is a canonical address conversion, not a general cwd fallback.
# 函数用途: 把绝对路径或用户根目录下的 tasks/... 路径转换成可比对的绝对写目标。
def _canonical_mutation_targets(
    raw_targets: list[str],
    *,
    owner_home: object,
) -> list[Path]:
    targets: list[Path] = []
    owner_root = _resolved_owner_home(owner_home)
    for raw in raw_targets:
        try:
            text = str(raw or "").strip()
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                if owner_root is None or not _is_canonical_owner_task_path(candidate):
                    continue
                candidate = owner_root / candidate
            targets.append(candidate.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return targets


# LLM: Durable thread owner_home is the only base allowed for owner-relative task addresses.
# 函数用途: 安全解析当前会话的用户根目录；缺失或非绝对路径时不提供回退。
def _resolved_owner_home(owner_home: object) -> Path | None:
    text = str(owner_home or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        return candidate.resolve(strict=False) if candidate.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None


# LLM: Canonical owner-relative task paths must stay under the literal tasks component.
# 函数用途: 拒绝普通相对路径和包含上下级跳转的路径，只接受 tasks/... 任务地址。
def _is_canonical_owner_task_path(candidate: Path) -> bool:
    parts = candidate.parts
    return bool(
        parts
        and parts[0] == "tasks"
        and all(part not in {"", ".", ".."} for part in parts)
    )


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


def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolHandlerOutcome:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolHandlerOutcome(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
        error_code="TOOL_ONE_SHOT_ALREADY_EXECUTED",
    )


def _finished_trace_request(runtime_request: ToolCallRuntimeRequest, result: ToolResult):
    return _finished_trace_request_from_trace(runtime_request.trace_request, result)


def _finished_trace_request_from_trace(trace_request: RunnerToolStageTraceRequest, result: ToolResult):
    return RunnerToolStageTraceRequest(
        agent=trace_request.agent,
        params=trace_request.params,
        tool_rounds=trace_request.tool_rounds,
        idx=trace_request.idx,
        call=trace_request.call,
        result=result,
    )


def _one_shot_result_consumes_key(result: ToolResult) -> bool:
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
