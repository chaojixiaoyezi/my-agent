from __future__ import annotations

"""Single durable lifecycle authority for one named Audit.

Preparation remains an ordinary Agent turn, while explicit start is a control
operation.  Both routes call this module so task identity, restart semantics,
workspace materialization, and runtime projection cannot drift by adapter.
"""

import time
from pathlib import Path

from ..common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_DEADLINE_ATTR,
    AUDIT_OBJECTIVE_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_RUN_PROMPT_ATTR,
    AUDIT_SOURCE_BINDINGS_ATTR,
    AUDIT_WINDOW_ATTR,
)
from ..ingestion.watch_state import close_prepare_watches_for_task
from ..user_space.run_workspace import EnsureRunWorkspaceRequest, activate_run_workspace
from .authority import (
    CONVERSATION_CANCELLATION_SCOPE_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_TURN_REQUEST_ID_ATTR,
    CONVERSATION_WORK_DURATION_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
    CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
)
from .models import THREAD_TASK_LINK_INACTIVE_STATUSES, new_id
from .workspace_paths import audit_workspace_path


class AuditLifecycleError(RuntimeError):
    """One fail-closed named-Audit lifecycle failure."""


def prepare_named_audit(
    agent: object,
    store: object,
    *,
    thread_id: str,
    work_name: str,
    prompt: str,
    prepare_request_id: str,
):
    """Reserve or reopen the exact named Audit for one ordinary prepare turn."""

    link = resolve_exact_active_audit(store, thread_id, work_name)
    reusable = (
        None
        if link is not None
        else resolve_reusable_audit(store, thread_id, work_name)
    )
    if link is not None:
        if not _same_prepare_request(link, prepare_request_id):
            updated = store.audits.record_pending_prompt(
                {
                    "task_id": link.task_id,
                    "prompt": prompt,
                    "prepare_request_id": prepare_request_id,
                }
            )
            if updated is None:
                raise AuditLifecycleError("Audit 准备内容未能可靠保存，请稍后重试")
            link = updated
    elif reusable is not None:
        reopened = store.audits.reopen_prepare(
            {
                "task_id": reusable.task_id,
                "prompt": prompt,
                "prepare_request_id": prepare_request_id,
            }
        )
        if reopened is None:
            raise AuditLifecycleError("Audit 准备状态发生冲突，请重试")
        link = reopened
    else:
        owner_home = _owner_home(agent)
        audit_id = new_id("audit")
        try:
            link = store.tasks.bind(
                {
                    "thread_id": thread_id,
                    "task_id": audit_id,
                    "goal": "",
                    "status": "preparing",
                    "task_path": str(audit_workspace_path(owner_home, audit_id)),
                    "work_kind": "audit",
                    "work_name": work_name,
                    "pending_prompt": prompt,
                    "pending_updated_at": time.time(),
                    "pending_prepare_request_id": prepare_request_id,
                    "cancellation_scope": "foreground",
                }
            )
        except ValueError:
            raced = resolve_exact_active_audit(store, thread_id, work_name)
            if raced is None:
                raise
            updated = store.audits.record_pending_prompt(
                {
                    "task_id": raced.task_id,
                    "prompt": prompt,
                    "prepare_request_id": prepare_request_id,
                }
            )
            if updated is None:
                raise AuditLifecycleError(
                    "Audit 准备内容未能可靠保存，请稍后重试"
                ) from None
            link = updated
    materialize_audit_workspace(agent, store, link, prompt=prompt)
    return store.tasks.load(link.task_id) or link


def _same_prepare_request(link: object, prepare_request_id: object) -> bool:
    """Make one recovered Gateway prepare reservation idempotent.

    The request may be retried after the durable Audit link was reserved but
    before conversation context could be loaded.  Replaying that exact request
    must not append the same user requirement a second time.
    """

    selected = str(prepare_request_id or "").strip()
    if not selected:
        return False
    return selected in {
        str(getattr(link, "pending_prepare_request_id", "") or "").strip(),
        str(getattr(link, "effective_prepare_request_id", "") or "").strip(),
    }


def start_named_audit(
    agent: object,
    store: object,
    *,
    thread_id: str,
    work_name: str,
    prompt: str,
    duration_seconds: int,
):
    """Start one exact named Audit without invoking a model presentation turn."""

    if duration_seconds <= 0:
        raise AuditLifecycleError("Audit 运行时长无效")
    link = resolve_exact_active_audit(store, thread_id, work_name)
    if link is not None:
        link = _settle_expired_audit_before_restart(
            agent,
            store,
            thread_id=thread_id,
            link=link,
        )
    status = str(getattr(link, "status", "") or "").strip().lower() if link else ""
    reusable = (
        resolve_reusable_audit(store, thread_id, work_name)
        if link is None or status in THREAD_TASK_LINK_INACTIVE_STATUSES
        else None
    )
    if link is not None and status not in {
        "preparing",
        *THREAD_TASK_LINK_INACTIVE_STATUSES,
    }:
        raise AuditLifecycleError(f"Audit“{work_name}”已经在运行。")
    if link is not None and status == "preparing":
        activated = store.audits.activate(
            {
                "task_id": link.task_id,
                "goal": prompt,
                "duration_seconds": duration_seconds,
            }
        )
        if activated is None:
            raise AuditLifecycleError("Audit 启动状态发生冲突，请重试")
    elif reusable is not None:
        activated = store.audits.reactivate(
            {
                "task_id": reusable.task_id,
                "goal": prompt,
                "duration_seconds": duration_seconds,
            }
        )
        if activated is None:
            raise AuditLifecycleError("Audit 重新启动状态发生冲突，请重试")
    else:
        owner_home = _owner_home(agent)
        audit_id = new_id("audit")
        current = time.time()
        try:
            activated = store.tasks.bind(
                {
                    "thread_id": thread_id,
                    "task_id": audit_id,
                    "goal": prompt,
                    "run_prompt": prompt,
                    "status": "active",
                    "task_path": str(audit_workspace_path(owner_home, audit_id)),
                    "work_kind": "audit",
                    "work_name": work_name,
                    "duration_seconds": duration_seconds,
                    "effective_revision": 1,
                    "effective_updated_at": current,
                    "run_epoch": 1,
                    "cancellation_scope": "detached",
                }
            )
        except ValueError as exc:
            raise AuditLifecycleError(f"Audit“{work_name}”已存在。") from exc
    _retire_prepare_watches_after_activation(agent, activated.task_id)
    materialize_audit_workspace(agent, store, activated, prompt=prompt)
    return store.tasks.load(activated.task_id) or activated


def resolve_exact_active_audit(store: object, thread_id: str, name: str):
    """Resolve one exact case-sensitive nonterminal Audit or fail closed."""

    links, errors = store.tasks.list_report(thread_id)
    if errors:
        raise AuditLifecycleError("Audit 状态当前不可用，请稍后重试")
    matches = [
        link
        for link in links
        if str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "") == name
        and str(getattr(link, "status", "") or "").strip().lower()
        not in THREAD_TASK_LINK_INACTIVE_STATUSES
    ]
    if len(matches) > 1:
        raise AuditLifecycleError("Audit 名称存在冲突，已停止本次操作")
    return matches[0] if matches else None


def resolve_reusable_audit(store: object, thread_id: str, name: str):
    """Select retained identity, preferring published configuration over recency."""

    links, errors = store.tasks.list_report(thread_id)
    if errors:
        raise AuditLifecycleError("Audit 历史状态当前不可用，请稍后重试")
    matches = [
        link
        for link in links
        if str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "") == name
        and str(getattr(link, "status", "") or "").strip().lower()
        in THREAD_TASK_LINK_INACTIVE_STATUSES
    ]
    if not matches:
        return None

    def priority(item: object) -> tuple[int, float, float, str]:
        bindings = tuple(getattr(item, "effective_source_bindings", ()) or ())
        published = bool(
            bindings
            or str(getattr(item, "effective_prepare_request_id", "") or "").strip()
            or str(getattr(item, "effective_user_prompt", "") or "").strip()
        )
        return (
            2 if bindings else 1 if published else 0,
            float(getattr(item, "expires_at", 0.0) or 0.0),
            float(getattr(item, "created_at", 0.0) or 0.0),
            str(getattr(item, "task_id", "") or ""),
        )

    return max(matches, key=priority)


def materialize_audit_workspace(
    agent: object,
    store: object,
    link: object,
    *,
    prompt: str,
) -> None:
    """Create/reuse the one durable Audit workspace and sync its task state."""

    root = str(getattr(link, "task_path", "") or "").strip()
    home = getattr(agent, "home_paths", None)
    if not root or home is None:
        raise AuditLifecycleError("Audit 工作目录当前不可用，请稍后重试")
    task_id = str(getattr(link, "task_id", "") or "").strip()
    run_epoch = max(0, int(getattr(link, "run_epoch", 0) or 0))
    activation_id = f"{task_id}:run:{run_epoch}" if run_epoch > 0 else task_id
    try:
        activate_run_workspace(
            root,
            EnsureRunWorkspaceRequest(
                home=str(getattr(home, "owner_home_dir", "") or ""),
                template="",
                task_name=f"Audit {getattr(link, 'work_name', '')}",
                user_prompt=str(getattr(link, "goal", "") or prompt),
                request_id=activation_id,
                run_id=activation_id,
                task_id=task_id,
                owner_id=str(getattr(home, "owner_id", "") or ""),
                owner_home=str(getattr(home, "owner_home_dir", "") or ""),
                source="conversation_audit",
            ),
        )
        store.tasks.bind(link.to_dict())
    except (OSError, TypeError, ValueError) as exc:
        raise AuditLifecycleError("Audit 工作目录无法可靠建立，请稍后重试") from exc


def audit_scope_payload(link: object) -> dict[str, object]:
    """Project one durable link into adapter-neutral typed runtime facts."""

    return {
        "audit_id": str(getattr(link, "task_id", "") or ""),
        "thread_id": str(getattr(link, "thread_id", "") or ""),
        "name": str(getattr(link, "work_name", "") or ""),
        "status": str(getattr(link, "status", "") or ""),
        "task_path": str(getattr(link, "task_path", "") or ""),
        "effective_prompt": str(getattr(link, "goal", "") or ""),
        "run_prompt": str(getattr(link, "run_prompt", "") or ""),
        "pending_prompt": str(getattr(link, "pending_prompt", "") or ""),
        "effective_revision": int(getattr(link, "effective_revision", 0) or 0),
        "run_epoch": max(0, int(getattr(link, "run_epoch", 0) or 0)),
        "duration_seconds": max(0, int(getattr(link, "duration_seconds", 0) or 0)),
        "expires_at": float(getattr(link, "expires_at", 0.0) or 0.0),
        "source_bindings": [
            dict(item)
            for item in (getattr(link, "effective_source_bindings", ()) or ())
            if isinstance(item, dict)
        ],
    }


def project_audit_runtime_attributes(
    base: dict[str, object] | None,
    scope: dict[str, object],
    *,
    thread_id: str = "",
    turn_request_id: str = "",
) -> dict[str, object]:
    """Merge one exact Audit scope into a run/tool context without prose inference."""

    selected = dict(base or {})
    audit_id = str(scope.get("audit_id") or "").strip()
    task_path = str(scope.get("task_path") or "").strip()
    if not audit_id or not task_path:
        return selected
    selected.pop(CONVERSATION_WORKSPACE_TASK_ID_ATTR, None)
    selected.pop(CONVERSATION_WORKSPACE_TASK_STATUS_ATTR, None)
    selected.pop(CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR, None)
    selected.pop(CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR, None)
    selected.pop("run_workspace", None)
    selected["conversation_task_id"] = audit_id
    selected[CONVERSATION_REQUEST_ID_ATTR] = audit_id
    if thread_id:
        selected["conversation_thread_id"] = thread_id
    if turn_request_id:
        selected[CONVERSATION_TURN_REQUEST_ID_ATTR] = turn_request_id
    selected[CONVERSATION_TRANSIENT_WORKSPACE_ATTR] = True
    selected[CONVERSATION_WORK_KIND_ATTR] = "audit"
    selected[CONVERSATION_WORK_NAME_ATTR] = str(scope.get("name") or "")
    selected[CONVERSATION_CANCELLATION_SCOPE_ATTR] = (
        "detached"
        if selected.get(AUDIT_ATTR) is True
        else str(selected.get(CONVERSATION_CANCELLATION_SCOPE_ATTR) or "foreground")
    )
    selected[AUDIT_RUN_EPOCH_ATTR] = max(0, int(scope.get("run_epoch") or 0))
    if selected.get(AUDIT_ATTR) is True:
        duration = max(0, int(scope.get("duration_seconds") or 0))
        deadline = float(scope.get("expires_at") or 0.0)
        if duration > 0:
            selected[AUDIT_WINDOW_ATTR] = duration
            selected[CONVERSATION_WORK_DURATION_ATTR] = duration
        if deadline > 0:
            selected[AUDIT_DEADLINE_ATTR] = deadline
        effective_prompt = str(scope.get("effective_prompt") or "").strip()
        if effective_prompt:
            selected[AUDIT_OBJECTIVE_ATTR] = effective_prompt
        run_prompt = str(scope.get("run_prompt") or "").strip()
        if run_prompt:
            selected[AUDIT_RUN_PROMPT_ATTR] = run_prompt
        bindings = scope.get("source_bindings")
        if isinstance(bindings, list):
            selected[AUDIT_SOURCE_BINDINGS_ATTR] = [
                dict(item) for item in bindings if isinstance(item, dict)
            ]
    selected["run_workspace"] = {
        "task_root": task_path,
        "output_dir": str(Path(task_path) / "output"),
        "work_dir": str(Path(task_path) / "work"),
    }
    selected[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    return selected


def _owner_home(agent: object) -> str:
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home:
        raise AuditLifecycleError("Audit 工作目录当前不可用，请稍后重试")
    return owner_home


def _settle_expired_audit_before_restart(
    agent: object,
    store: object,
    *,
    thread_id: str,
    link: object,
):
    if str(getattr(link, "status", "") or "").strip().lower() != "active":
        return link
    try:
        expires_at = float(getattr(link, "expires_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return link
    if expires_at <= 0 or expires_at > time.time():
        return link
    task_id = str(getattr(link, "task_id", "") or "").strip()
    if not task_id or not thread_id:
        return link
    from .task_promotion import complete_current_conversation_task

    completed = complete_current_conversation_task(
        agent,
        {
            "conversation_task_id": task_id,
            "conversation_thread_id": thread_id,
        },
        source="background_main_agent",
    )
    if not completed:
        return link
    try:
        refreshed = store.tasks.load(task_id)
    except Exception:
        return link
    return refreshed if refreshed is not None else link


def _retire_prepare_watches_after_activation(agent: object, task_id: object) -> None:
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    selected_task = str(task_id or "").strip()
    if not owner_home or not selected_task:
        return
    try:
        close_prepare_watches_for_task(Path(owner_home), selected_task)
    except (OSError, RuntimeError, ValueError):
        return


__all__ = [
    "AuditLifecycleError",
    "audit_scope_payload",
    "materialize_audit_workspace",
    "prepare_named_audit",
    "project_audit_runtime_attributes",
    "resolve_exact_active_audit",
    "resolve_reusable_audit",
    "start_named_audit",
]
