from __future__ import annotations

"""Structured lifecycle authority for one named durable conversation work item."""

import json
import threading
from dataclasses import dataclass
from typing import Any

from ..concurrency.interrupt import interrupt_by_name
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .control_commands import conversation_request_interrupt_name
from .models import THREAD_TASK_LINK_INACTIVE_STATUSES


@dataclass(frozen=True)
class NamedWorkStopResult:
    ok: bool
    kind: str
    name: str
    task_id: str = ""
    error_code: str = ""


class StopNamedWorkTool(BaseTool):
    """Let the main agent honor an ordinary-language request to stop one named item."""

    model_spec = ToolModelSpec(
        name="stop_named_work",
        description=(
            "Stop or cancel one exact user-named persistent Audit or Goal in the current conversation. "
            "Use it when the user explicitly names the work to stop; it does not stop the foreground reply."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["audit", "goal"], "description": "Optional persistent work kind."},
                "name": {"type": "string", "minLength": 1, "maxLength": 64, "description": "Exact user-visible work name."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="conversation",
            use_cases=(
                "The user explicitly asks to stop or cancel an exact named Audit or Goal",
                "用户说停止某个已命名的长期审计或目标",
            ),
            avoid_when=(
                "The user asks to stop only the current reply",
                "The user did not identify an exact persistent work name",
            ),
            keywords=("stop named audit", "stop named goal", "cancel persistent work", "停止命名审计", "取消命名目标"),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("dangerous"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("kind", "name")),
    )

    def __init__(self, agent: object):
        self.agent = agent

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        run_params = getattr(self.agent, "_current_run_params", None)
        attrs = getattr(run_params, "task_attributes", None)
        thread_id = (
            str(attrs.get("conversation_thread_id") or "").strip()
            if isinstance(attrs, dict)
            else ""
        )
        kind = str(params.get("kind") or "").strip().lower()
        name = str(params.get("name") or "").strip()
        if not thread_id:
            return _tool_result(
                False,
                kind=kind,
                name=name,
                error_code="CONVERSATION_CONTEXT_REQUIRED",
            )
        stopped = stop_named_conversation_work(
            self.agent,
            thread_id=thread_id,
            kind=kind,
            name=name,
        )
        return _tool_result(
            stopped.ok,
            kind=stopped.kind,
            name=stopped.name,
            error_code=stopped.error_code,
        )


def stop_named_conversation_work(
    agent: object,
    *,
    thread_id: str,
    kind: str,
    name: str,
) -> NamedWorkStopResult:
    """Stop one owner/thread-scoped work item selected by exact structured fields."""
    selected_kind = str(kind or "").strip().lower()
    selected_name = str(name or "").strip()
    if selected_kind not in {"", "audit", "goal"} or not selected_name:
        return NamedWorkStopResult(
            False,
            selected_kind,
            selected_name,
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return NamedWorkStopResult(
            False,
            selected_kind,
            selected_name,
            error_code="CONVERSATION_TASK_STATE_UNAVAILABLE",
        )
    try:
        if not selected_kind:
            selected_kind, error_code = _resolve_named_work_kind(
                store,
                thread_id,
                selected_name,
            )
            if not selected_kind:
                return NamedWorkStopResult(
                    False,
                    "",
                    selected_name,
                    error_code=error_code,
                )
        if selected_kind == "goal":
            task_id, error_code = _remove_named_goal(store, thread_id, selected_name)
        else:
            task_id, error_code = _select_named_audit(store, thread_id, selected_name)
        if not task_id:
            return NamedWorkStopResult(
                False,
                selected_kind,
                selected_name,
                error_code=error_code,
            )
        # LLM: Named-work closure and Audit source-open commit share the exact
        # task transition guard.  Whichever transition wins first leaves one
        # durable authority for the loser to observe; a late source cannot
        # escape the watch-close snapshot after its parent was cancelled.
        # 函数用途: 串行化命名任务终止与来源正式启用，堵住 clear 同时 open 的漏网子代理。
        with store.task_transition_guard(task_id):
            _cancel_task_projection(store, task_id)
            _disable_task_policies(store, task_id)
            stop_named_work_runtime(
                agent,
                task_id=task_id,
                kind=selected_kind,
            )
        return NamedWorkStopResult(True, selected_kind, selected_name, task_id=task_id)
    except Exception:
        return NamedWorkStopResult(
            False,
            selected_kind,
            selected_name,
            error_code="CONVERSATION_TASK_STATE_UNAVAILABLE",
        )


def _resolve_named_work_kind(
    store: object,
    thread_id: str,
    name: str,
) -> tuple[str, str]:
    goals = store.load_goals(thread_id)
    goal_matches = [
        goal
        for goal in goals
        if str(getattr(goal, "name", "") or "").casefold() == name.casefold()
        and str(getattr(goal, "status", "") or "") != "complete"
    ]
    links, errors = store.task_links_report(thread_id)
    if errors:
        return "", "CONVERSATION_TASK_STATE_UNAVAILABLE"
    audit_matches = [
        link
        for link in links
        if str(getattr(link, "work_kind", "") or "") == "audit"
        and str(getattr(link, "work_name", "") or "") == name
        and str(getattr(link, "status", "") or "").strip().lower()
        not in THREAD_TASK_LINK_INACTIVE_STATUSES
    ]
    matches = [
        *(["goal"] if len(goal_matches) == 1 else []),
        *(["audit"] if len(audit_matches) == 1 else []),
    ]
    if len(goal_matches) > 1 or len(audit_matches) > 1 or len(matches) > 1:
        return "", "NAMED_WORK_CONFLICT"
    return (matches[0], "") if matches else ("", "NAMED_WORK_NOT_FOUND")


def _remove_named_goal(store: object, thread_id: str, name: str) -> tuple[str, str]:
    with store.goal_transition_guard(thread_id):
        goals = store.load_goals(thread_id)
        matching = [
            goal
            for goal in goals
            if str(getattr(goal, "name", "") or "").casefold() == name.casefold()
            and str(getattr(goal, "status", "") or "") != "complete"
        ]
        if len(matching) > 1:
            return "", "NAMED_WORK_CONFLICT"
        if not matching:
            return "", "NAMED_WORK_NOT_FOUND"
        goal = matching[0]
        deleted = store.delete_goal(thread_id, expected_goal_id=goal.goal_id)
        return (
            (str(getattr(deleted, "task_id", "") or ""), "")
            if deleted is not None
            else ("", "NAMED_WORK_CONFLICT")
        )


def _select_named_audit(store: object, thread_id: str, name: str) -> tuple[str, str]:
    links, errors = store.task_links_report(thread_id)
    if errors:
        return "", "CONVERSATION_TASK_STATE_UNAVAILABLE"
    matching = [
        link
        for link in links
        if str(getattr(link, "work_kind", "") or "") == "audit"
        and str(getattr(link, "work_name", "") or "") == name
        and str(getattr(link, "status", "") or "").strip().lower()
        not in THREAD_TASK_LINK_INACTIVE_STATUSES
    ]
    if len(matching) > 1:
        return "", "NAMED_WORK_CONFLICT"
    return (
        (str(getattr(matching[0], "task_id", "") or ""), "")
        if matching
        else ("", "NAMED_WORK_NOT_FOUND")
    )


def _cancel_task_projection(store: object, task_id: str) -> None:
    store.update_task_status({"task_id": task_id, "status": "cancelled"})


def _disable_task_policies(store: object, task_id: str) -> None:
    for policy in store.list_progress_policies(enabled_only=True):
        if str(getattr(policy, "task_id", "") or "") == task_id:
            store.disable_progress_policy(policy.policy_id)


def _interrupt_registry(agent: object, task_id: str) -> None:
    try:
        registry = agent.local_store.task_registry
        current = registry.lookup_task(task_id)
        status = str((current or {}).get("status") or "").strip()
        if status:
            registry.update_task_status(task_id, "cancelled", expected_status=status)
    except Exception:
        return


def _cancel_named_work_subagents_async(
    agent: object,
    task_id: str,
    *,
    kind: str,
) -> None:
    def cancel() -> None:
        try:
            run_ids = agent.subagent_run_ids_for_request(task_id)
        except Exception:
            return
        from ..agent_core.orchestration.tools.cancel import (
            CancelSubagentTaskRequest,
            cancel_subagent_task,
        )
        from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

        for run_id in run_ids:
            try:
                task = agent.subagents.load(run_id)
            except Exception:
                continue
            is_audit_source = _is_audit_source_worker(task)
            if kind == "audit":
                if not _audit_worker_can_stop_after_clear(
                    agent,
                    task,
                    task_id=task_id,
                ):
                    continue
            # A source runner can finish after the watch is durably closed but
            # before this asynchronous tree walk reaches it.  BLOCKED/DONE is
            # therefore not an escape hatch from exact named Audit clear:
            # typed source workers whose exact watch is closed are projected
            # to CANCELLED even if their final model turn arrived late.
            if task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES):
                if kind != "audit" or not is_audit_source:
                    continue
                if str(getattr(task, "status", "") or "").upper() == "CANCELLED":
                    continue
            try:
                cancel_subagent_task(
                    agent,
                    CancelSubagentTaskRequest(
                        task=task,
                        reason=f"named_{kind}_clear",
                        source="named_work_lifecycle",
                    ),
                )
            except Exception:
                continue

    threading.Thread(
        target=cancel,
        name=f"clear-named-work-{task_id}",
        daemon=True,
    ).start()


def _is_audit_source_worker(task: object) -> bool:
    from ..common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    return structured_audit_source_worker_attributes(
        getattr(task, "attributes", {}) or {}
    )


def _audit_worker_can_stop_after_clear(
    agent: object,
    task: object,
    *,
    task_id: str,
) -> bool:
    """Fail closed only for typed source workers whose exact watch is not closed."""
    from ..common.audit_activation import (
        AUDIT_SOURCE_WATCH_ID_ATTR,
        structured_audit_source_worker_attributes,
    )

    attrs = getattr(task, "attributes", {}) or {}
    if not structured_audit_source_worker_attributes(attrs):
        return True
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
        or ""
    ).strip()
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()
    if not owner_home or not watch_id:
        return False
    from pathlib import Path

    from ..ingestion.watch_state import load_state

    state = load_state(Path(owner_home), watch_id)
    return bool(
        state is not None
        and state.closed
        and state.audit_guarantee
        and state.audit_root_task_id == task_id
    )


# LLM: This is the single runtime-stop path for one already-resolved durable
# work id. Audit watches are closed before descendant discovery, preventing a
# concurrent source-worker ensure from escaping the cancellation snapshot.
# 函数用途: 在任务名称已经解析成稳定 task_id 后，按类型停止它的运行现场；Audit 会先关来源再取消整棵子代理树。
def stop_named_work_runtime(
    agent: object,
    *,
    task_id: str,
    kind: str,
) -> tuple[str, ...]:
    selected_task = str(task_id or "").strip()
    selected_kind = str(kind or "").strip().lower()
    if not selected_task or selected_kind not in {"audit", "goal"}:
        return ()
    interrupt_by_name(conversation_request_interrupt_name(selected_task))
    _interrupt_registry(agent, selected_task)
    closed_watches = (
        close_named_audit_watches(agent, selected_task)
        if selected_kind == "audit"
        else ()
    )
    _cancel_named_work_subagents_async(
        agent,
        selected_task,
        kind=selected_kind,
    )
    return closed_watches


# LLM: Audit watch selection is delegated to the ingestion ledger and uses only the exact typed root task id.
# 函数用途: 命名 Audit 被停止时关闭它自己的数据流；原始 spool/archive 保留，不影响同用户其他 Audit。
def close_named_audit_watches(
    agent: object,
    task_id: str,
    *,
    reason: str = "named_audit_clear",
) -> tuple[str, ...]:
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
        or ""
    ).strip()
    if not owner_home or not str(task_id or "").strip():
        return ()
    from pathlib import Path

    from ..ingestion.watch_state import close_audit_watches_for_task

    return close_audit_watches_for_task(
        Path(owner_home),
        task_id,
        reason=reason,
    )


def _tool_result(
    ok: bool,
    *,
    kind: str,
    name: str,
    error_code: str = "",
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "stop_named_work",
        ok,
        json.dumps(
            {
                "ok": ok,
                "kind": kind,
                "name": name,
                "error_code": error_code,
            },
            ensure_ascii=False,
        ),
        error_code=error_code,
    )


__all__ = [
    "NamedWorkStopResult",
    "StopNamedWorkTool",
    "close_named_audit_watches",
    "stop_named_work_runtime",
    "stop_named_conversation_work",
]
