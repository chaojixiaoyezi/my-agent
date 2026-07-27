from __future__ import annotations

"""Conversation-parent authority for starting or recovering subagent runs."""

from dataclasses import asdict, dataclass
from typing import Any

from ....conversation.models import (
    THREAD_TASK_LINK_ACTIVE_STATUS,
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
)
from ....subagents.recovery_eligibility import user_stopped_run_is_resumable

ALLOW = "allow"
CANCEL = "cancel"
HOLD = "hold"


@dataclass(frozen=True)
class ConversationLifecycleDecision:
    run_id: str
    action: str
    reason: str
    thread_id: str = ""
    parent_task_id: str = ""
    parent_status: str = ""
    task_status: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def should_cancel(self) -> bool:
        return self.action == CANCEL

    @property
    def parent_allows_children(self) -> bool:
        return (
            self.reason in {"unscoped", "parent_execution_active"}
            or self.parent_status == THREAD_TASK_LINK_ACTIVE_STATUS
        )

    def payload(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items() if str(value)}


@dataclass(frozen=True)
class _TaskConversationScope:
    run_id: str
    thread_id: str
    parent_task_id: str
    user_stopped_resume_allowed: bool = False

    def decision(
        self,
        action: str,
        reason: str,
        statuses: tuple[str, str] = ("", ""),
    ) -> ConversationLifecycleDecision:
        return ConversationLifecycleDecision(
            self.run_id,
            action,
            reason,
            self.thread_id,
            self.parent_task_id,
            statuses[0],
            statuses[1],
        )


@dataclass(frozen=True)
class _ThreadLinkSnapshot:
    links_by_id: dict[str, object]
    duplicate_ids: set[str]
    error_ids: set[str]


def conversation_lifecycle_decisions(
    agent: object,
    tasks: list[object],
    *,
    resume_run_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, ConversationLifecycleDecision]:
    """Resolve a batch once per thread; missing structured authority fails closed."""
    decisions, scoped = _partition_task_scopes(tasks, resume_run_ids=resume_run_ids)
    store = getattr(agent, "conversation_store", None)
    report = getattr(store, "task_links_report", None)
    for thread_id, scopes in scoped.items():
        if not callable(report):
            _hold_scopes(decisions, scopes, "conversation_store_unavailable")
            continue
        snapshot = _load_thread_snapshot(report, thread_id)
        if snapshot is None:
            _hold_scopes(decisions, scopes, "conversation_thread_unavailable")
            continue
        execution = (
            _thread_execution_state(store, thread_id)
            if _completed_parent_projection_present(snapshot, scopes)
            else None
        )
        for scope in scopes:
            decisions[scope.run_id] = _scoped_decision(scope, snapshot, execution)
    return decisions


def _partition_task_scopes(
    tasks: list[object],
    *,
    resume_run_ids: set[str] | frozenset[str],
) -> tuple[dict[str, ConversationLifecycleDecision], dict[str, list[_TaskConversationScope]]]:
    decisions: dict[str, ConversationLifecycleDecision] = {}
    scoped: dict[str, list[_TaskConversationScope]] = {}
    for task in tasks:
        scope, immediate = _task_scope(task, resume_run_ids=resume_run_ids)
        if immediate is not None:
            decisions[scope.run_id] = immediate
            continue
        scoped.setdefault(scope.thread_id, []).append(scope)
    return decisions, scoped


def _task_scope(
    task: object,
    *,
    resume_run_ids: set[str] | frozenset[str],
) -> tuple[_TaskConversationScope, ConversationLifecycleDecision | None]:
    attrs = getattr(task, "attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    run_id = _task_id(task)
    scope = _TaskConversationScope(
        run_id,
        str(attrs.get("conversation_thread_id") or "").strip(),
        str(attrs.get("conversation_task_id") or "").strip(),
        run_id in resume_run_ids and user_stopped_run_is_resumable(task),
    )
    if not scope.thread_id and not scope.parent_task_id:
        return scope, scope.decision(ALLOW, "unscoped")
    if not scope.thread_id or not scope.parent_task_id:
        return scope, scope.decision(HOLD, "conversation_identity_incomplete")
    return scope, None


def _load_thread_snapshot(report: Any, thread_id: str) -> _ThreadLinkSnapshot | None:
    try:
        links, load_errors = report(thread_id)
    except Exception:
        return None
    links_by_id, duplicate_ids = _links_by_id(links)
    error_ids = {
        str(item.get("task_id") or "").strip()
        for item in load_errors
        if isinstance(item, dict) and str(item.get("task_id") or "").strip()
    }
    return _ThreadLinkSnapshot(links_by_id, duplicate_ids, error_ids)


def _scoped_decision(
    scope: _TaskConversationScope,
    snapshot: _ThreadLinkSnapshot,
    execution: dict[str, object] | None,
) -> ConversationLifecycleDecision:
    _parent, parent_status, blocked = _required_link(scope, snapshot, scope.parent_task_id, "parent")
    if blocked is not None:
        return blocked
    parent_execution_active = False
    if parent_status in {"completed", "done"}:
        if execution is None or execution.get("state_available") is not True:
            return scope.decision(HOLD, "parent_execution_state_unavailable", (parent_status, ""))
        running_task_ids = execution.get("running_task_ids")
        parent_execution_active = (
            scope.parent_task_id in running_task_ids
            if isinstance(running_task_ids, list)
            else False
        )
    if parent_status in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES:
        if not parent_execution_active:
            return scope.decision(CANCEL, "parent_link_closed", (parent_status, ""))
    elif parent_status != THREAD_TASK_LINK_ACTIVE_STATUS:
        return scope.decision(HOLD, "parent_link_not_active", (parent_status, ""))
    _child, child_status, blocked = _required_link(scope, snapshot, scope.run_id, "run")
    if blocked is not None:
        return scope.decision(blocked.action, blocked.reason, (parent_status, child_status))
    if child_status in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES:
        if child_status == "cancelled" and scope.user_stopped_resume_allowed:
            return scope.decision(
                ALLOW,
                "user_stopped_same_run_resume",
                (parent_status, child_status),
            )
        return scope.decision(CANCEL, "run_link_closed", (parent_status, child_status))
    action = ALLOW if child_status == THREAD_TASK_LINK_ACTIVE_STATUS else HOLD
    if action == ALLOW:
        reason = "parent_execution_active" if parent_execution_active else "conversation_links_active"
    else:
        reason = "run_link_not_active"
    return scope.decision(action, reason, (parent_status, child_status))


def _thread_execution_state(
    store: object,
    thread_id: str,
) -> dict[str, object]:
    """Read the same structured execution authority used by status/steer/stop."""
    from ....conversation.task_promotion import conversation_thread_execution_state

    try:
        return conversation_thread_execution_state(store, thread_id)
    except Exception as exc:
        return {
            "running_task_ids": [],
            "state_available": False,
            "load_errors": [
                {
                    "context": "conversation.lifecycle.parent_execution",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ],
        }


def _completed_parent_projection_present(
    snapshot: _ThreadLinkSnapshot,
    scopes: list[_TaskConversationScope],
) -> bool:
    return any(
        _link_status(snapshot.links_by_id.get(scope.parent_task_id)) in {"completed", "done"}
        for scope in scopes
        if scope.parent_task_id not in snapshot.duplicate_ids
        and scope.parent_task_id not in snapshot.error_ids
    )


def _required_link(
    scope: _TaskConversationScope,
    snapshot: _ThreadLinkSnapshot,
    task_id: str,
    role: str,
) -> tuple[object | None, str, ConversationLifecycleDecision | None]:
    if task_id in snapshot.duplicate_ids:
        return None, "", scope.decision(HOLD, "conversation_link_duplicate")
    if task_id in snapshot.error_ids:
        return None, "", scope.decision(HOLD, "conversation_link_unreadable")
    link = snapshot.links_by_id.get(task_id)
    if link is None:
        return None, "", scope.decision(HOLD, f"{role}_link_missing")
    status = _link_status(link)
    if not _same_thread(link, scope.thread_id):
        return link, status, scope.decision(HOLD, f"{role}_link_thread_mismatch")
    return link, status, None


def _links_by_id(links: object) -> tuple[dict[str, object], set[str]]:
    result: dict[str, object] = {}
    duplicates: set[str] = set()
    for link in links if isinstance(links, list) else []:
        task_id = str(getattr(link, "task_id", "") or "").strip()
        if not task_id:
            continue
        if task_id in result:
            duplicates.add(task_id)
        result[task_id] = link
    return result, duplicates


def _hold_scopes(
    decisions: dict[str, ConversationLifecycleDecision],
    scopes: list[_TaskConversationScope],
    reason: str,
) -> None:
    for scope in scopes:
        decisions[scope.run_id] = scope.decision(HOLD, reason)


def _same_thread(link: object, thread_id: str) -> bool:
    return str(getattr(link, "thread_id", "") or "").strip() == thread_id


def _link_status(link: object) -> str:
    return str(getattr(link, "status", "") or "").strip().lower()


def _task_id(task: object) -> str:
    return str(getattr(task, "id", "") or "").strip()


__all__ = [
    "ConversationLifecycleDecision",
    "conversation_lifecycle_decisions",
]
