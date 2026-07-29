from __future__ import annotations

"""LLM: 任务状态只由结构化运行结果、task link 与子代理状态改变，禁止解析自然语言触发。

模块用途: 在普通会话真的开始工作时提升任务、明确选择旧任务，并在正常 turn 结束后关闭候选。
"""

import hashlib
import time
from pathlib import Path

from .authority import (
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
    CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
)


# LLM: A sticky cwd is not a live-task capability.  Reuse an active exact task when supplied, but
# start a fresh request task over terminal workspace state; new_task remains a structured decision.
# 函数用途: 本轮真正工作时建立运行身份；保留目录连续性，绝不隐式复活终态任务。
def promote_current_conversation_task(
    agent: object,
    *,
    goal: str = "",
    new_task: bool = False,
):
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return None
    if new_task:
        return _promote_new_conversation_task(agent, current, attrs, goal=goal)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    explicit_task_id = str(attrs.get("conversation_task_id") or "").strip()
    task_id = str(
        explicit_task_id
        or getattr(current, "task_id", "")
        or getattr(current, "run_id", "")
        or getattr(current, "request_id", "")
        or ""
    ).strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None or not callable(getattr(store, "bind_task", None)):
        return None
    from ..agent_core.runner.context import current_subagent_run_id

    child_run_id = current_subagent_run_id(agent)
    if existing := _active_conversation_link(store, thread_id, task_id):
        attrs["conversation_task_id"] = existing.task_id
        # Child identity and cwd are separate structured facts.  A child verifies
        # that its parent conversation task is still active, but it must not replace
        # its own (or an exact locally rebound) workspace with the parent's cwd.
        if child_run_id:
            return existing if _publish_current_request_task_binding(current, existing) else None
        workspace = _selected_task_workspace(existing.task_path)
        if workspace is not None:
            _set_current_task_workspace(agent, attrs, workspace)
        selected = _materialize_promoted_workspace(agent, current, existing, task_goal=goal) or existing
        return _activate_current_conversation_task(store, current, attrs, selected)
    if explicit_task_id and not child_run_id:
        selected = _selectable_conversation_link(store, thread_id, explicit_task_id)
        if str(getattr(selected, "status", "") or "").strip().lower() in {
            "completed",
            "interrupted",
        }:
            return select_current_conversation_task(agent, explicit_task_id)
    task_goal = str(
        goal
        or getattr(current, "root_user_prompt", "")
        or getattr(current, "prompt", "")
        or task_id
    ).strip()
    try:
        link = store.bind_task(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "goal": task_goal,
                "status": "active",
            }
        )
    except Exception:
        return None
    # task_attributes 是本轮结构化状态；后续派工、wait、workspace writer 都从这里继承，
    # 不再从用户自然语言猜“是不是任务”。
    attrs["conversation_task_id"] = task_id
    selected = _materialize_promoted_workspace(agent, current, link, task_goal=task_goal) or link
    return _activate_current_conversation_task(store, current, attrs, selected)


# LLM: A structured new_task=true starts from the current request identity and switches the sticky
# workspace only after normal promotion succeeds; failures restore the prior turn attributes.
# 函数用途: 显式开始全新任务，同时保留失败前原工作目录，避免半切换状态。
def _promote_new_conversation_task(
    agent: object,
    current: object,
    attrs: dict[str, object],
    *,
    goal: str,
):
    original_attrs = dict(attrs)
    original_workspace = str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
    prior_root = _workspace_task_root(attrs.get("run_workspace")) or original_workspace
    for key in (
        "conversation_task_id",
        "conversation_task_completed",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR,
        CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR,
        CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR,
        CONVERSATION_WORKSPACE_TASK_ID_ATTR,
        CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
        "run_workspace",
        "conversation_rebase_from_task_root",
    ):
        attrs.pop(key, None)
    agent._current_run_task_workspace = ""
    selected = promote_current_conversation_task(agent, goal=goal)
    if selected is None:
        attrs.clear()
        attrs.update(original_attrs)
        agent._current_run_task_workspace = original_workspace
        return None
    selected_root = _workspace_task_root(attrs.get("run_workspace"))
    if prior_root and selected_root and prior_root != selected_root:
        attrs["conversation_rebase_from_task_root"] = prior_root
    return selected


# LLM: Promotion succeeds only after the exact task workspace is durably selected for future turns
# and the gateway request lineage is published; this flag is intentionally per-turn, not persisted.
# 函数用途: 完成一次任务激活，记住会话工作目录并标记本轮确实做过工作。
def _activate_current_conversation_task(
    store: object,
    current: object,
    attrs: dict[str, object],
    link: object,
):
    if not _remember_conversation_workspace(store, link):
        return None
    attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    return link if _publish_current_request_task_binding(current, link) else None


def _materialize_promoted_workspace(
    agent: object,
    current: object,
    link: object,
    *,
    task_goal: str,
):
    """把已晋升会话任务绑定到真实 workspace；失败时保留任务链接但不伪造路径。"""
    try:
        from ..agent_core.run_task_workspace_writer import materialize_promoted_task_workspace

        workspace = materialize_promoted_task_workspace(agent, current, task_goal)
    except OSError:
        return None
    if workspace is None:
        return None
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return link
    try:
        return store.bind_task(
            {
                "thread_id": str(getattr(link, "thread_id", "") or ""),
                "task_id": str(getattr(link, "task_id", "") or ""),
                "goal": str(getattr(link, "goal", "") or task_goal),
                "status": str(getattr(link, "status", "") or "active"),
                "task_path": str(workspace),
            }
        )
    except OSError:
        return link


# LLM: 只有精确 task_id 且 status=active 的结构化链接才可续接。
# 函数用途: 在指定会话中查找一个仍活跃的任务链接。
def _active_conversation_link(store: object, thread_id: str, task_id: str):
    try:
        links, errors = store.active_task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next(
        (
            item
            for item in links
            if item.task_id == task_id and str(item.status or "").strip().lower() == "active"
        ),
        None,
    )


# LLM: Selection uses an exact structured task id and never matches prose.  A terminal ordinary
# task yields a fresh execution successor over the same cwd; only a paused structured /goal resumes
# in place because the goal record itself owns that durable task id.
# 函数用途: 选择既有工作目录；普通终态任务保持终态，新一轮另建运行身份。
def select_current_conversation_task(agent: object, task_id: str):
    """由模型通过结构化工具明确选择当前会话中的既有任务，不解析用户文本。"""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    selected_id = str(task_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not selected_id or store is None:
        return None
    link = _selectable_conversation_link(store, thread_id, selected_id)
    if link is None:
        return None
    if conversation_task_selection_blocker(agent, selected_id) is not None:
        return None
    prior_current_id = str(attrs.get("conversation_task_id") or "").strip()
    link = _activate_selectable_link(agent, store, link)
    if link is None or not _supersede_prior_current(store, thread_id, prior_current_id, selected_id):
        return None
    attrs["conversation_task_id"] = link.task_id
    workspace = _selected_task_workspace(link.task_path)
    if workspace is None:
        link = _materialize_promoted_workspace(
            agent,
            current,
            link,
            task_goal=str(getattr(link, "goal", "") or selected_id),
        ) or link
        workspace = _selected_task_workspace(link.task_path)
    if workspace is not None:
        _set_current_task_workspace(agent, attrs, workspace)
    if not _remember_conversation_workspace(store, link):
        return None
    attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    return link if _publish_current_request_task_binding(current, link) else None


def rebase_subagent_conversation_workspace(agent: object, link: object) -> bool:
    """Bind a child runner to an exact workspace without changing parent task state.

    A subagent has its own runner identity and working directory, while
    ``conversation_task_id`` remains the parent-task lineage it reports into.  This
    mirrors the separate parent-id/cwd fields used by 会话运行时 and the separate
    parent-session/child-session fields used by 通道运行时.  Rebinding a child cwd must
    therefore never reopen, supersede, or select a conversation task globally.
    """
    from ..agent_core.runner.context import current_subagent_run_id

    if not current_subagent_run_id(agent):
        return False
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return False
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id or thread_id != str(getattr(link, "thread_id", "") or "").strip():
        return False
    workspace = _selected_task_workspace(getattr(link, "task_path", ""))
    if workspace is None:
        return False
    _set_current_task_workspace(agent, attrs, workspace)
    attrs["conversation_subagent_workspace_rebase"] = {
        "task_id": str(getattr(link, "task_id", "") or "").strip(),
        "task_root": str(workspace),
    }
    return True


def _publish_current_request_task_binding(current: object, link: object) -> bool:
    """Persist exact live-turn lineage through the gateway-supplied typed callback."""
    callback = getattr(current, "conversation_task_binding_callback", None)
    if callback is None:
        return True
    if not callable(callback):
        return False
    try:
        return callback(link) is True
    except Exception:
        return False


# LLM: Persist only an exact existing task path through ConversationStore's single workspace writer.
# 函数用途: 把本轮选中的任务目录记到 thread，供停止、完成或重启后的下一轮继续继承。
def _remember_conversation_workspace(store: object, link: object) -> bool:
    workspace = _selected_task_workspace(getattr(link, "task_path", ""))
    writer = getattr(store, "select_workspace_task", None)
    if workspace is None or not callable(writer):
        return False
    try:
        thread = writer(
            {
                "thread_id": str(getattr(link, "thread_id", "") or ""),
                "task_id": str(getattr(link, "task_id", "") or ""),
            }
        )
    except Exception:
        return False
    return str(getattr(thread, "workspace_task_id", "") or "") == str(
        getattr(link, "task_id", "") or ""
    )


def _set_current_task_workspace(agent: object, attrs: dict[str, object], workspace: Path) -> None:
    previous_workspace = _workspace_task_root(attrs.get("run_workspace")) or str(
        getattr(agent, "_current_run_task_workspace", "") or ""
    ).strip()
    if previous_workspace and previous_workspace != str(workspace):
        attrs["conversation_rebase_from_task_root"] = previous_workspace
    attrs["run_workspace"] = {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }
    # 一轮内后续工具仍持有同一个 agent；同步唯一当前工作区，确保派工、finding 与
    # 动态 write boundary 不会继续引用本轮刚创建的占位目录。
    agent._current_run_task_workspace = str(workspace)


# LLM: Exact terminal selection forks a fresh execution generation unless a persisted /goal record
# requires its original task id.  A new active turn retains persistent history and working directory.
# 函数用途: 激活所选工作；普通任务续做不改变旧终态，特殊持续目标才原位恢复。
def _activate_selectable_link(agent: object, store: object, link: object):
    prior_status = str(getattr(link, "status", "") or "").strip().lower()
    if prior_status not in {
        "completed",
        "interrupted",
    }:
        return link
    goal_resume = _selected_goal_requires_in_place_resume(store, link)
    if goal_resume is None:
        return None
    if not goal_resume:
        return _continue_terminal_link_as_new_execution(agent, store, link)
    try:
        reopened = store.update_task_status({"task_id": link.task_id, "status": "active"})
    except Exception:
        return None
    if reopened is None:
        return None
    if not _resume_matching_selected_goal(agent, store, reopened):
        try:
            store.update_task_status(
                {
                    "task_id": link.task_id,
                    "status": prior_status,
                    "expected_status": "active",
                }
            )
        except Exception:
            pass
        return None
    try:
        registry = agent.local_store.task_registry
        current = registry.lookup_task(link.task_id)
        status = str((current or {}).get("status") or "").strip()
        if status:
            registry.update_task_status(link.task_id, "running", expected_status=status)
    except Exception:
        pass
    return reopened


def _selected_goal_requires_in_place_resume(store: object, link: object) -> bool | None:
    """Return whether an exact paused /goal record owns the selected terminal task."""
    try:
        goal = store.load_goal(str(getattr(link, "thread_id", "") or ""))
    except Exception:
        return None
    if goal is None or str(getattr(goal, "task_id", "") or "") != str(
        getattr(link, "task_id", "") or ""
    ):
        return False
    return str(getattr(goal, "status", "") or "").strip().lower() in {
        "active",
        "blocked",
        "paused",
        "usage_limited",
    }


def _continue_terminal_link_as_new_execution(agent: object, store: object, link: object):
    """Create one idempotent successor task over the selected terminal workspace."""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return None
    thread_id = str(getattr(link, "thread_id", "") or "").strip()
    source_id = str(getattr(link, "task_id", "") or "").strip()
    base_id = str(
        getattr(current, "task_id", "")
        or getattr(current, "run_id", "")
        or getattr(current, "request_id", "")
        or ""
    ).strip()
    if not thread_id or not source_id or not base_id:
        return None
    successor_id = _terminal_successor_task_id(
        store,
        thread_id,
        base_id=base_id,
        source_id=source_id,
        task_path=str(getattr(link, "task_path", "") or ""),
    )
    if not successor_id:
        return None
    try:
        successor = store.bind_task(
            {
                "thread_id": thread_id,
                "task_id": successor_id,
                "goal": str(getattr(link, "goal", "") or source_id),
                "status": "active",
                "task_path": str(getattr(link, "task_path", "") or ""),
            }
        )
    except Exception:
        return None
    attrs["conversation_selected_from_task_id"] = source_id
    return successor


def _terminal_successor_task_id(
    store: object,
    thread_id: str,
    *,
    base_id: str,
    source_id: str,
    task_path: str,
) -> str:
    """Choose a stable request-local successor id without mutating an existing other task."""
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return ""
    if errors:
        return ""
    by_id = {
        str(getattr(item, "task_id", "") or ""): item
        for item in links
        if str(getattr(item, "task_id", "") or "")
    }
    existing = by_id.get(base_id)
    if existing is None:
        return base_id
    if (
        str(getattr(existing, "status", "") or "").strip().lower() == "active"
        and str(getattr(existing, "task_path", "") or "") == task_path
    ):
        return base_id
    suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
    derived = f"{base_id}-continue-{suffix}"
    existing = by_id.get(derived)
    if existing is None:
        return derived
    if (
        str(getattr(existing, "status", "") or "").strip().lower() == "active"
        and str(getattr(existing, "task_path", "") or "") == task_path
    ):
        return derived
    return ""


def _resume_matching_selected_goal(agent: object, store: object, link: object) -> bool:
    """Exact task selection reactivates its paused goal without parsing user prose."""
    thread_id = str(getattr(link, "thread_id", "") or "").strip()
    task_id = str(getattr(link, "task_id", "") or "").strip()
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not thread_id or not task_id or not isinstance(attrs, dict):
        return False
    try:
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id)
            if goal is None or str(getattr(goal, "task_id", "") or "").strip() != task_id:
                return True
            status = str(getattr(goal, "status", "") or "").strip().lower()
            if status in {"active", "complete"}:
                return True
            if status not in {"paused", "blocked", "usage_limited"}:
                return False
            updated = store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": "active",
                    "expected_status": status,
                }
            )
    except Exception:
        return False
    if updated is None or str(getattr(updated, "status", "") or "").strip().lower() != "active":
        return False
    attrs["thread_goal_id"] = str(getattr(updated, "goal_id", "") or "")
    attrs["thread_goal_activation_pending"] = True
    return True


def _supersede_prior_current(
    store: object,
    thread_id: str,
    prior_current_id: str,
    selected_id: str,
) -> bool:
    if not prior_current_id or prior_current_id == selected_id:
        return True
    if _active_conversation_link(store, thread_id, prior_current_id) is None:
        return True
    try:
        return store.update_task_status(
            {"task_id": prior_current_id, "status": "superseded"}
        ) is not None
    except Exception:
        return False


def _selectable_conversation_link(store: object, thread_id: str, task_id: str):
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next(
        (
            item
            for item in links
            if item.task_id == task_id
            and is_user_selectable_conversation_task(item)
        ),
        None,
    )


def is_user_selectable_conversation_task(link: object) -> bool:
    task_id = str(getattr(link, "task_id", "") or "").strip().lower()
    status = str(getattr(link, "status", "") or "").strip().lower()
    return status in {"active", "completed", "interrupted"} and not task_id.startswith(
        ("subagent-", "bg-main-")
    )


def conversation_task_selection_blocker(agent: object, task_id: str) -> dict[str, object] | None:
    """Block a second executor while an exact active task already has durable execution."""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    selected_id = str(task_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not selected_id or store is None:
        return None
    link = _selectable_conversation_link(store, thread_id, selected_id)
    if link is None or str(getattr(link, "status", "") or "").strip().lower() != "active":
        return None
    state = conversation_task_execution_state(store, thread_id, selected_id)
    if state["state_available"] is True and state["running"] is not True:
        return None
    return state


def conversation_task_execution_state(store: object, thread_id: str, task_id: str) -> dict[str, object]:
    """Read structured policies and the thread claim; never infer execution from user text."""
    thread_state = conversation_thread_execution_state(store, thread_id)
    sources_by_task_id = thread_state.get("sources_by_task_id")
    sources_by_task_id = sources_by_task_id if isinstance(sources_by_task_id, dict) else {}
    sources = sources_by_task_id.get(str(task_id or ""), [])
    sources = [str(item) for item in sources] if isinstance(sources, list) else []
    return {
        "running": bool(sources),
        "state_available": thread_state.get("state_available") is True,
        "sources": sources,
        "load_errors": list(thread_state.get("load_errors") or []),
    }


def conversation_thread_execution_state(store: object, thread_id: str) -> dict[str, object]:
    """Read all running task ids for one thread with one policy and claim snapshot."""
    sources_by_task_id: dict[str, list[str]] = {}
    load_errors: list[dict[str, object]] = []
    _append_thread_policy_execution_state(
        store,
        str(thread_id or ""),
        sources_by_task_id,
        load_errors,
    )
    _append_thread_claim_execution_state(
        store,
        str(thread_id or ""),
        sources_by_task_id,
        load_errors,
    )
    normalized_sources = {
        task_id: list(dict.fromkeys(sources))
        for task_id, sources in sources_by_task_id.items()
        if task_id and sources
    }
    return {
        "running_task_ids": list(normalized_sources),
        "state_available": not load_errors,
        "sources_by_task_id": normalized_sources,
        "load_errors": load_errors,
    }


def _append_thread_policy_execution_state(
    store: object,
    thread_id: str,
    sources_by_task_id: dict[str, list[str]],
    load_errors: list[dict[str, object]],
) -> None:
    loader = getattr(store, "list_progress_policies_report", None)
    if not callable(loader):
        load_errors.append(
            {
                "context": "conversation.task_execution.policies",
                "error": "progress policy loader is unavailable",
            }
        )
        return
    try:
        policies, errors = loader(enabled_only=True)
    except Exception as exc:
        load_errors.append(
            {"context": "conversation.task_execution.policies", "error": str(exc)}
        )
        return
    load_errors.extend(item for item in errors if isinstance(item, dict))
    for item in policies:
        if str(getattr(item, "thread_id", "") or "") != thread_id:
            continue
        _append_execution_source(
            sources_by_task_id,
            str(getattr(item, "task_id", "") or ""),
            "progress_policy",
        )


def _append_thread_claim_execution_state(
    store: object,
    thread_id: str,
    sources_by_task_id: dict[str, list[str]],
    load_errors: list[dict[str, object]],
) -> None:
    loader = getattr(store, "load_background_run_claim_report", None)
    if not callable(loader):
        load_errors.append(
            {
                "context": "conversation.task_execution.claim",
                "error": "background claim loader is unavailable",
            }
        )
        return
    try:
        claim, error = loader(thread_id)
    except Exception as exc:
        load_errors.append(
            {"context": "conversation.task_execution.claim", "error": str(exc)}
        )
        return
    if isinstance(error, dict):
        load_errors.append(error)
        return
    if not isinstance(claim, dict):
        return
    try:
        expires_at = float(claim.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        load_errors.append(
            {
                "context": "conversation.task_execution.claim",
                "error": "background claim expiry is invalid",
            }
        )
        return
    if (
        str(claim.get("status") or "").strip().lower() == "running"
        and expires_at > time.time()
    ):
        _append_execution_source(
            sources_by_task_id,
            str(claim.get("task_id") or ""),
            "background_claim",
        )


def _append_execution_source(
    sources_by_task_id: dict[str, list[str]],
    task_id: str,
    source: str,
) -> None:
    selected_id = str(task_id or "").strip()
    if selected_id:
        sources_by_task_id.setdefault(selected_id, []).append(source)


# LLM: This gate reads only exact task/workspace state.  A resolved terminal cwd may start a fresh
# execution, while an already-running active task blocks a second executor.
# 函数用途: 区分“目录已选定”和“任务正在运行”，避免旧终态复活或同目录双执行。
def conversation_workspace_decision(agent: object) -> dict[str, object] | None:
    current = getattr(agent, "_current_run_params", None)
    # A task-local child carries the parent conversation ids only for lineage,
    # wake routing, and archive projection.  It owns an independent runner
    # lane, so the main conversation's live executor must not block its tools.
    if str(getattr(current, "context_scope", "") or "").strip().lower() == "task_local":
        return None
    attrs = getattr(current, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not thread_id:
        return None
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None
    current_task_id = str(attrs.get("conversation_task_id") or "").strip()
    if current_task_id:
        if attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is True:
            return None
        state = conversation_task_execution_state(store, thread_id, current_task_id)
        if state["state_available"] is True and state["running"] is not True:
            return None
        return {
            "ok": False,
            "error": (
                "The selected conversation task already has a live executor."
                if state["state_available"] is True
                else "The selected conversation task execution state could not be read safely."
            ),
            "thread_id": thread_id,
            "task_id": current_task_id,
            "running": state["running"],
            "state_available": state["state_available"],
            "sources": state["sources"],
            "load_errors": state["load_errors"],
            "how_to_fix": (
                "Keep this turn as ordinary chat. Use /btw to steer the active run or /stop "
                "before starting a different execution path."
            ),
        }
    workspace_task_id = str(attrs.get(CONVERSATION_WORKSPACE_TASK_ID_ATTR) or "").strip()
    if workspace_task_id and _workspace_task_root(attrs.get("run_workspace")):
        # The gateway already resolved an exact owner-local cwd.  A terminal
        # source task contributes only that directory; promotion will bind the
        # current request id as a fresh task and leave the source terminal.
        return None
    try:
        links, load_errors = store.task_links_report(thread_id)
    except Exception as exc:
        return {
            "ok": False,
            "error": "The conversation workspace index could not be read.",
            "thread_id": thread_id,
            "load_errors": [f"{type(exc).__name__}: {exc}"],
            "candidates": [],
        }
    candidates = [
        {
            "task_id": str(link.task_id or ""),
            "status": str(link.status or ""),
            "goal": str(link.goal or ""),
            "task_path": str(link.task_path or ""),
            "created_at": float(getattr(link, "created_at", 0.0) or 0.0),
        }
        for link in links
        if is_user_selectable_conversation_task(link)
    ]
    candidates.sort(key=lambda item: float(item["created_at"]), reverse=True)
    if not candidates and not load_errors:
        return None
    return {
        "ok": False,
        "error": "Choose the conversation workspace before starting work.",
        "thread_id": thread_id,
        "candidates": candidates[:12],
        "load_errors": [item for item in load_errors if isinstance(item, dict)],
        "how_to_fix": (
            "To continue existing work, call task_progress action=select with the exact candidate task_id. "
            "For genuinely new work, call task_progress action=start with new_task=true. "
            "Do not call a work tool first."
        ),
    }


# LLM: 仅由正常 runtime turn 终态调用；此函数自身不读取最终回复正文。
# 函数用途: 在没有待处理引导、活跃目标或未终态子代理时关闭当前普通会话任务。
def complete_current_conversation_task(
    agent: object,
    task_attributes: object,
    *,
    source: str = "",
    current_task_id: str = "",
) -> bool:
    """在结构化运行终态后关闭当前会话任务候选。"""
    # 子代理会继承 conversation_task_id，方便它把产物和进度归回父任务；这不等于它
    # 拥有关闭父会话任务的权力。只认结构化 run source，绝不从完成文案猜角色。
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    run_source = str(source or "").strip().lower()
    if (
        attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is not True
        and not run_source.startswith("subagent_")
        and run_source != "background_main_agent"
    ):
        return False
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    run_task_id = str(current_task_id or "").strip()
    if not run_source:
        return False
    # 子代理继承父 conversation_task_id 时，不得关闭父任务；如果系统为子代理
    # 建了与其自身 task_id 完全相同的会话链接，则允许它关闭自己的链接，避免
    # DONE 子任务永久残留在普通聊天的 active candidates 中。
    if run_source.startswith("subagent_") and (not run_task_id or run_task_id != task_id):
        return False
    store = getattr(agent, "conversation_store", None)
    if not task_id or not thread_id or store is None:
        return False
    try:
        with store.task_transition_guard(task_id):
            # `/goal` 的每一轮也会正常结束当前模型 turn，但“本轮有最终回复”不等于
            # “整个持续目标已经达成”。活跃目标只能由 update_goal 明确写入终态；
            # 目标记录损坏时同样 fail-closed，不能趁读取失败误关根任务。
            goal = store.load_goal(thread_id)
            if goal is not None and goal.task_id == task_id and goal.status != "complete":
                return False
            link = _active_conversation_link(store, thread_id, task_id)
            if (
                link is None
                or store.pending_guidance("task", task_id, limit=1)
                or _conversation_task_has_open_subagents(agent, task_id)
            ):
                return False
            updated = store.update_task_status(
                {
                    "task_id": task_id,
                    "status": "completed",
                    "expected_status": "active",
                }
            )
    except Exception:
        return False
    if updated is None:
        return False
    attrs["conversation_task_completed"] = True
    return True


# LLM: Open-child authority comes from canonical subagent statuses and exact request lineage.
# 函数用途: 判断当前根任务是否仍有未终态子代理；状态读取失败时 fail-closed 保持任务活跃。
def _conversation_task_has_open_subagents(agent: object, task_id: str) -> bool:
    try:
        from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

        run_ids = set(agent.subagent_run_ids_for_request(task_id))
        if not run_ids:
            return False
        runs = list(agent.subagents.list_runs())
    except Exception:
        return True
    indexed = {
        str(getattr(run, "id", "") or "").strip(): run
        for run in runs
        if str(getattr(run, "id", "") or "").strip()
    }
    return any(
        run_id not in indexed
        or not task_status_in(getattr(indexed[run_id], "status", ""), SUBAGENT_ENDED_STATUSES)
        for run_id in run_ids
    )


# LLM: task_path 只接受链接中的结构化路径，解析失败或路径不存在时不伪造 workspace。
# 函数用途: 恢复已选择任务的真实工作区路径。
def _selected_task_workspace(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve(strict=False)
    except OSError:
        return None
    return path if path.exists() else None


def _workspace_task_root(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("task_root") or "").strip()


def rebase_selected_conversation_workspace_params(agent: object, value: object) -> object:
    """Rebase structured tool arguments from this turn's placeholder into the selected task root."""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    if not isinstance(attrs, dict):
        return value
    source = str(attrs.get("conversation_rebase_from_task_root") or "").strip().rstrip("/\\")
    target = _workspace_task_root(attrs.get("run_workspace")).rstrip("/\\")
    if not source or not target or source == target:
        return value
    return _rebase_workspace_value(value, source, target)


def _rebase_workspace_value(value: object, source: str, target: str) -> object:
    if isinstance(value, dict):
        return {key: _rebase_workspace_value(item, source, target) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebase_workspace_value(item, source, target) for item in value]
    if isinstance(value, tuple):
        return tuple(_rebase_workspace_value(item, source, target) for item in value)
    if not isinstance(value, str):
        return value
    if value == source:
        return target
    # 只替换完整目录前缀；不会把相似任务 id（如 req_1 与 req_10）误改。
    return value.replace(f"{source}/", f"{target}/").replace(
        f"{source}\\", f"{target}\\"
    )


__all__ = [
    "complete_current_conversation_task",
    "conversation_workspace_decision",
    "conversation_task_execution_state",
    "conversation_thread_execution_state",
    "conversation_task_selection_blocker",
    "is_user_selectable_conversation_task",
    "promote_current_conversation_task",
    "rebase_selected_conversation_workspace_params",
    "select_current_conversation_task",
]
