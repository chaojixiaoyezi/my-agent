from __future__ import annotations

"""LLM: 任务状态只由结构化运行结果、task link 与子代理状态改变，禁止解析自然语言触发。

模块用途: 在普通会话真的开始工作时提升任务、绑定连续工作目录，并在正常 turn 结束后关闭候选。
"""

import hashlib
import time
from pathlib import Path

from .authority import (
    CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR,
    CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR,
    CONVERSATION_CANCELLATION_SCOPE_ATTR,
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_WORK_DURATION_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
)


# LLM: A thread's latest workspace projection is not a live-task capability. The first work tool
# binds a fresh request unless exact request/Goal/active authority already selected a workspace.
# 函数用途: 本轮真正工作时自动建立运行身份；只有结构化授权才复用旧目录，不让旧清单控制新一轮。
def promote_current_conversation_task(
    agent: object,
    *,
    goal: str = "",
):
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return None
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
    if attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True and not child_run_id:
        loader = getattr(store, "load_task_link", None)
        try:
            transient = loader(task_id) if callable(loader) else None
        except Exception:
            transient = None
        if (
            transient is None
            or str(getattr(transient, "thread_id", "") or "") != thread_id
            or str(getattr(transient, "task_id", "") or "") != task_id
        ):
            return None
        workspace = _selected_task_workspace(getattr(transient, "task_path", ""))
        if workspace is None:
            return None
        _set_current_task_workspace(agent, attrs, workspace)
        attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
        return transient if _publish_current_request_task_binding(current, transient) else None
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
    if not child_run_id:
        # Gateway 在首个模型采样前只把 exact bound request、Goal 或 active task
        # 写入该字段。thread.workspace_task_id 同时保留给 UI/定位，但 completed/
        # interrupted sticky 不能在这里偷偷取得新任务的执行选择权。
        candidate = str(attrs.get(CONVERSATION_WORKSPACE_TASK_ID_ATTR) or explicit_task_id).strip()
        if candidate:
            reusable = _reusable_conversation_workspace_link(store, thread_id, candidate)
            if reusable is not None and not _detached_named_work_link(reusable):
                bound = bind_current_conversation_workspace(agent, candidate)
                if bound is not None:
                    return bound
                # 精确 task 续接失败时不另建影子目录。active 任务由
                # execution blocker 收口；terminal 任务则由新执行代 successor 原子换代。
                return None
            if not str(
                goal
                or getattr(current, "root_user_prompt", "")
                or getattr(current, "prompt", "")
                or ""
            ).strip():
                return None
        # detached 任务(后台巡检等)只保留历史目录投影,不续接:新请求是独立的
        # 前台任务,按原路径新建 link(不占用 detached 任务的工作区)。
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
                "work_kind": str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or ""),
                "work_name": str(attrs.get(CONVERSATION_WORK_NAME_ATTR) or ""),
                "duration_seconds": attrs.get(CONVERSATION_WORK_DURATION_ATTR),
                "cancellation_scope": str(
                    attrs.get(CONVERSATION_CANCELLATION_SCOPE_ATTR) or "foreground"
                ),
            }
        )
    except Exception:
        return None
    # task_attributes 是本轮结构化状态；后续派工、wait、workspace writer 都从这里继承，
    # 不再从用户自然语言猜“是不是任务”。
    attrs["conversation_task_id"] = task_id
    selected = _materialize_promoted_workspace(agent, current, link, task_goal=task_goal) or link
    return _activate_current_conversation_task(store, current, attrs, selected)


# LLM: Promotion succeeds only after the exact task workspace is durably bound for future turns
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


# LLM: Materialization must publish one atomic workspace fact set. The generic workspace writer
# fills run_workspace/delivery refs, while this conversation seam also owns execution_cwd,
# runtime roots and provider-path rebase metadata; never leave those projections split.
# 函数用途: 为刚晋升的会话任务创建或复用目录，并同步本轮所有工具与子代理使用的唯一工作区。
def _materialize_promoted_workspace(
    agent: object,
    current: object,
    link: object,
    *,
    task_goal: str,
):
    """把已晋升会话任务绑定到真实 workspace；失败时保留任务链接但不伪造路径。"""
    attrs = getattr(current, "task_attributes", None)
    pre_materialize_workspace = (
        _workspace_task_root(attrs.get("run_workspace"))
        or str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
        or str(attrs.get(CONVERSATION_EXECUTION_CWD_ATTR) or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    inherited = _selected_task_workspace(getattr(link, "task_path", ""))
    if inherited is not None and isinstance(attrs, dict):
        # 链接已持有既有目录(续接/新执行代数继承原目录):先填进本轮工作区,
        # 下面的 materialize 命中 existing 分支只刷新身份文件(state.json/
        # task.yaml),绝不另建新目录——否则「同 cwd 新执行代数」承诺被打破,
        # 每轮续跑散落新目录(问题5 影子任务的目录分裂源)。
        _set_current_task_workspace(agent, attrs, inherited)
    try:
        from ..agent_core.run_task_workspace_writer import materialize_promoted_task_workspace

        workspace = materialize_promoted_task_workspace(agent, current, task_goal)
    except OSError:
        return None
    if workspace is None:
        return None
    if isinstance(attrs, dict):
        _set_current_task_workspace(
            agent,
            attrs,
            Path(workspace).expanduser().resolve(strict=False),
            rebase_from=pre_materialize_workspace,
        )
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
                "work_kind": str(getattr(link, "work_kind", "") or ""),
                "work_name": str(getattr(link, "work_name", "") or ""),
                "duration_seconds": getattr(link, "duration_seconds", None),
                "expires_at": getattr(link, "expires_at", None),
                "cancellation_scope": str(
                    getattr(link, "cancellation_scope", "") or "foreground"
                ),
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


# LLM: Detached named work keeps its own lifecycle and cannot become foreground cwd authority.
# 函数用途: 判断 Audit/后台巡检等独立任务，避免把其目录自动交给普通前台消息。
def _detached_named_work_link(link: object) -> bool:
    """detached 命名工作(audit/后台巡检)只保留导航投影，不参与前台续接。"""
    return str(getattr(link, "cancellation_scope", "") or "").strip().lower() == "detached"


# LLM: This reads the latest task projection for bookkeeping only; callers must not treat it as
# execution authority without an exact request/Goal/active decision.
# 函数用途: 读取线程最近任务投影，供回绑时淘汰本轮占位记录，不直接决定工作目录。
def _sticky_workspace_task_id(store: object, thread_id: str) -> str:
    """读取线程最近任务投影；仅用于替换占位 link，不直接授予下一轮 cwd。"""
    loader = getattr(store, "load_thread", None)
    if not callable(loader):
        return ""
    try:
        thread = loader(thread_id)
    except Exception:
        return ""
    if thread is None:
        return ""
    return str(getattr(thread, "workspace_task_id", "") or "").strip()


# LLM: Binding uses an exact structured task id and never matches prose. A terminal task selected
# by exact authority yields a fresh execution successor over the same cwd; only a paused structured
# /goal resumes in place because the goal record itself owns that durable task id.
# 函数用途: 内部绑定既有工作目录；普通终态任务保持终态，新一轮另建运行身份。
def bind_current_conversation_workspace(agent: object, task_id: str):
    """由结构化会话身份或精确写入路径绑定既有目录，不作为模型工具暴露。"""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    selected_id = str(task_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not selected_id or store is None:
        return None
    link = _reusable_conversation_workspace_link(store, thread_id, selected_id)
    if link is None:
        return None
    if conversation_task_execution_blocker(agent, selected_id) is not None:
        return None
    source_workspace = (
        _workspace_task_root(attrs.get("run_workspace"))
        or str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
        or str(attrs.get(CONVERSATION_EXECUTION_CWD_ATTR) or "").strip()
    )
    # 先前的 current 是线程最近任务投影,不是本轮 gateway 派生的占位 id:
    # 占位 id 从未 activate 过,把它当 prior 会在 supersede 时误杀同 id 的
    # 新执行代数(问题5 影子任务:successor 刚建就被标 superseded)。
    prior_current_id = _sticky_workspace_task_id(store, thread_id) or str(
        attrs.get("conversation_task_id") or ""
    ).strip()
    link = _activate_reusable_workspace_link(agent, store, link)
    if link is None or not _supersede_prior_current(store, thread_id, prior_current_id, selected_id):
        return None
    attrs["conversation_task_id"] = link.task_id
    workspace = _selected_task_workspace(link.task_path)
    # A terminal ordinary run stays terminal while this request becomes a fresh
    # execution generation over the same cwd.  Refresh the canonical workspace
    # identity files even when the directory already exists; otherwise state.json
    # and task.yaml would still name the historical run.
    if link.task_id != selected_id or workspace is None:
        link = _materialize_promoted_workspace(
            agent,
            current,
            link,
            task_goal=str(getattr(link, "goal", "") or selected_id),
        ) or link
        workspace = _selected_task_workspace(link.task_path)
    if workspace is not None:
        _rebind_current_task_progress(
            agent,
            current,
            attrs,
            source_workspace=source_workspace,
            target_workspace=str(workspace),
        )
        _set_current_task_workspace(agent, attrs, workspace)
    if not _remember_conversation_workspace(store, link):
        return None
    attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    return link if _publish_current_request_task_binding(current, link) else None


# LLM: A main-turn exact workspace successor changes the canonical task-path progress key.
# Transfer only the same request's display generation; a missing or stale source is a no-op,
# while failures remain structured diagnostics and never grant workspace authority.
# 函数用途: 续作回绑时同步搬迁本轮 Todo，让后续按原 ID 更新仍命中同一本清单。
def _rebind_current_task_progress(
    agent: object,
    current: object,
    attrs: dict[str, object],
    *,
    source_workspace: str,
    target_workspace: str,
) -> None:
    source = str(source_workspace or "").strip()
    target = str(target_workspace or "").strip()
    if not source or not target or source == target:
        return
    from ..agent_core.runtime.owner_roots import runtime_owner_root
    from ..agent_core.runtime.task_identity import (
        progress_display_generation_id,
        task_path_progress_ledger_id,
    )
    from ..task_progress import rebind_task_progress_display_plan

    result = rebind_task_progress_display_plan(
        runtime_owner_root(agent),
        task_path_progress_ledger_id(source),
        task_path_progress_ledger_id(target),
        generation_id=progress_display_generation_id(agent, current),
    )
    if str(result.get("status") or "") not in {
        "same_ledger",
        "source_missing",
        "generation_mismatch",
    }:
        attrs["conversation_task_progress_rebind"] = result


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


# LLM: First promotion rebases provider-generated paths from the trusted client cwd into the
# canonical owner/task workspace. Later reselection rebases from the previous task root.
# 函数用途: 切换本轮唯一任务目录，并登记已有工具参数需要从哪个旧根重定向。
def _set_current_task_workspace(
    agent: object,
    attrs: dict[str, object],
    workspace: Path,
    *,
    rebase_from: str = "",
) -> None:
    previous_workspace = str(rebase_from or "").strip() or (
        _workspace_task_root(attrs.get("run_workspace"))
        or str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
        or str(attrs.get(CONVERSATION_EXECUTION_CWD_ATTR) or "").strip()
    )
    if previous_workspace and previous_workspace != str(workspace):
        attrs["conversation_rebase_from_task_root"] = previous_workspace
    attrs["run_workspace"] = {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }
    attrs[CONVERSATION_EXECUTION_CWD_ATTR] = str(workspace)
    attrs[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] = [str(workspace)]
    # 一轮内后续工具仍持有同一个 agent；同步唯一当前工作区，确保派工、finding 与
    # 动态 write boundary 不会继续引用本轮刚创建的占位目录。
    agent._current_run_task_workspace = str(workspace)


# LLM: Exact terminal workspace binding forks a fresh execution generation unless a persisted /goal
# requires its original task id.  A new active turn retains persistent history and working directory.
# 函数用途: 激活已绑定目录；普通任务续做不改变旧终态，特殊持续目标才原位恢复。
def _activate_reusable_workspace_link(agent: object, store: object, link: object):
    prior_status = str(getattr(link, "status", "") or "").strip().lower()
    if prior_status not in {
        "completed",
        "interrupted",
    }:
        return link
    goal_resume = _bound_goal_requires_in_place_resume(store, link)
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
    if not _resume_matching_bound_goal(agent, store, reopened):
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


def _bound_goal_requires_in_place_resume(store: object, link: object) -> bool | None:
    """Return whether an exact paused /goal record owns the bound terminal task."""
    try:
        goal = store.load_goal(
            str(getattr(link, "thread_id", "") or ""),
            task_id=str(getattr(link, "task_id", "") or ""),
        )
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
    """Create one idempotent successor task over the bound terminal workspace."""
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
    # A terminal link selected by exact authority contributes its canonical cwd. The successor
    # is a new 会话运行时 active turn, so its objective must come from this turn's exact
    # user input rather than the historical execution that previously occupied
    # the directory.  Copying the old goal here makes foreground execution look
    # correct while a later background wake resumes unrelated historical work.
    current_goal = str(
        getattr(current, "root_user_prompt", "")
        or getattr(current, "user_prompt", "")
        or getattr(current, "prompt", "")
        or ""
    ).strip()
    if not current_goal:
        return None
    try:
        successor = store.bind_task(
            {
                "thread_id": thread_id,
                "task_id": successor_id,
                "goal": current_goal,
                "status": "active",
                "task_path": str(getattr(link, "task_path", "") or ""),
            }
        )
    except Exception:
        return None
    attrs["conversation_continued_from_task_id"] = source_id
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


def _resume_matching_bound_goal(agent: object, store: object, link: object) -> bool:
    """Exact workspace binding reactivates its paused goal without parsing user prose."""
    thread_id = str(getattr(link, "thread_id", "") or "").strip()
    task_id = str(getattr(link, "task_id", "") or "").strip()
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not thread_id or not task_id or not isinstance(attrs, dict):
        return False
    try:
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id, task_id=task_id)
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


def _reusable_conversation_workspace_link(store: object, thread_id: str, task_id: str):
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
            and is_reusable_conversation_workspace(item)
        ),
        None,
    )


def is_reusable_conversation_workspace(link: object) -> bool:
    task_id = str(getattr(link, "task_id", "") or "").strip().lower()
    status = str(getattr(link, "status", "") or "").strip().lower()
    return status in {"active", "completed", "interrupted"} and not task_id.startswith(
        ("subagent-", "bg-main-")
    )


def conversation_task_execution_blocker(agent: object, task_id: str) -> dict[str, object] | None:
    """Block a second executor while an exact active task already has durable execution."""
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    selected_id = str(task_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not selected_id or store is None:
        return None
    link = _reusable_conversation_workspace_link(store, thread_id, selected_id)
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
        metadata = getattr(item, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        if str(metadata.get("kind") or "") == "subagent_progress_watch":
            # wait 登记的是「等某 run 的进度」:被 watch 的 run 才是执行中的对象,
            # task_id 只是调度归属(通常=父任务自身),不能作为 running 源——
            # 否则父任务唤醒轮续接会被自己的 wait policy 判成 running,被
            # workspace blocker 拦成 CONVERSATION_TASK_BINDING_FAILED 死锁
            # (真机 2026-08-09 requests 复刻:claim 驱动轮 sticky 回落 bind 被拦,
            # 父代理全部工具禁足,连 cancel policy 都救不了自己)。
            # self-watch(watch_run_id 空或=task_id)不证明任何 run 在执行。
            watched = str(metadata.get("watch_run_id") or "").strip()
            task_id_field = str(getattr(item, "task_id", "") or "").strip()
            if not watched or watched == task_id_field:
                continue
            _append_execution_source(sources_by_task_id, watched, "progress_policy")
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


# LLM: This gate reads only exact execution state.  Historical task candidates never block a new
# turn; only a genuinely live executor may prevent concurrent mutation of the same workspace.
# 函数用途: 只阻止同目录双执行，不要求用户或模型选择、开始、关闭旧任务。
def conversation_workspace_execution_blocker(agent: object) -> dict[str, object] | None:
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
                "The current conversation workspace already has a live executor."
                if state["state_available"] is True
                else "The current conversation workspace execution state could not be read safely."
            ),
            "thread_id": thread_id,
            "task_id": current_task_id,
            "running": state["running"],
            "state_available": state["state_available"],
            "sources": state["sources"],
            "load_errors": state["load_errors"],
            "how_to_fix": (
                "Do not launch a second mutating executor in the same workspace. "
                "The conversation remains available; steer or stop the current turn, or wait for it to finish."
            ),
        }
    return None


# LLM: 仅由正常 runtime turn 终态调用；此函数自身不读取最终回复正文。后台
# child lifecycle 回合还必须来自终态树快照，不能用采样期间才变化的新状态误关根任务。
# 函数用途: 在没有待处理引导、活跃目标、未终态子代理或陈旧阶段快照时关闭普通会话任务。
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
    if _background_child_phase_requires_fresh_turn(attrs, run_source):
        return False
    # 子代理继承父 conversation_task_id 时，不得关闭父任务；如果系统为子代理
    # 建了与其自身 task_id 完全相同的会话链接，则允许它关闭自己的链接，避免
    # DONE 子任务永久残留在普通聊天的 active candidates 中。
    if run_source.startswith("subagent_") and (not run_task_id or run_task_id != task_id):
        return False
    store = getattr(agent, "conversation_store", None)
    if not task_id or not thread_id or store is None:
        return False
    completed_work_kind = ""
    audit_activation_ready = True
    try:
        with store.task_transition_guard(task_id):
            # `/goal` 的每一轮也会正常结束当前模型 turn，但“本轮有最终回复”不等于
            # “整个持续目标已经达成”。活跃目标只能由 update_goal 明确写入终态；
            # 目标记录损坏时同样 fail-closed，不能趁读取失败误关根任务。
            goal = store.load_goal(thread_id, task_id=task_id)
            if goal is not None and goal.task_id == task_id and goal.status != "complete":
                return False
            link = _active_conversation_link(store, thread_id, task_id)
            if (
                link is None
                or store.pending_guidance("task", task_id, limit=1)
                or _conversation_task_has_open_subagents(agent, task_id)
                or conversation_task_has_unseen_lifecycle_wakes(
                    store,
                    task_id,
                    active_wake_signal_ids=_active_background_wake_signal_ids(attrs),
                )
            ):
                return False
            completed_work_kind = (
                str(getattr(link, "work_kind", "") or "").strip().lower()
            )
            if completed_work_kind == "audit":
                try:
                    expires_at = float(getattr(link, "expires_at", 0.0) or 0.0)
                except (TypeError, ValueError):
                    expires_at = 0.0
                from ..ingestion.audit_state import (
                    audit_task_activation_facts,
                    task_has_incomplete_watch,
                )
                from ..ingestion.source_worker import audit_task_has_unreported_findings

                if (
                    expires_at > time.time()
                    or task_has_incomplete_watch(agent, task_id)
                    or audit_task_has_unreported_findings(agent, task_id)
                    or _audit_owner_report_pending(store, task_id)
                ):
                    return False
                audit_activation_ready = (
                    audit_task_activation_facts(agent, link).get("ready") is True
                )
            updated = store.update_task_status(
                {
                    "task_id": task_id,
                    "status": (
                        "completed"
                        if completed_work_kind != "audit" or audit_activation_ready
                        else "failed"
                    ),
                    "expected_status": "active",
                }
            )
    except Exception:
        return False
    if updated is None:
        return False
    if completed_work_kind == "audit":
        from .named_work import close_named_audit_watches

        close_named_audit_watches(
            agent,
            task_id,
            reason=(
                "audit_window_settled"
                if audit_activation_ready
                else "audit_window_incomplete"
            ),
        )
    attrs["conversation_task_completed"] = True
    return True


# LLM: This is an event-freshness fence, not an acceptance gate. A turn that
# started from a partial child tree may report or integrate what it saw, but a
# later child terminal edge must get its own model sampling boundary before the
# durable root can close.
# 函数用途: 防止后台模型只看见部分子代理完成，却因采样期间最后一个子代理结束而误关根任务。
def _background_child_phase_requires_fresh_turn(
    attrs: dict[str, object],
    run_source: str,
) -> bool:
    if run_source != "background_main_agent":
        return False
    phase = str(attrs.get(CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR) or "").strip()
    return phase in {"subagents_active", "subagent_state_unknown"}


# LLM: Child lifecycle envelopes are durable parent-mailbox obligations. A task
# may ignore only the exact envelopes sampled by its current background turn;
# read failures and every other same-root lifecycle wake keep closeout blocked.
# 函数用途: 判断当前根任务是否还有尚未交给主代理的子代理生命周期信封。
def conversation_task_has_unseen_lifecycle_wakes(
    store: object,
    task_id: str,
    *,
    active_wake_signal_ids: tuple[str, ...] = (),
) -> bool:
    selected = str(task_id or "").strip()
    if not selected or store is None:
        return True
    loader = getattr(store, "pending_wake_signals_report", None)
    try:
        if callable(loader):
            signals, load_errors = loader(limit=0)
            if load_errors:
                return True
        else:
            signals = store.pending_wake_signals(limit=0)
    except Exception:
        return True
    from .models import SUBAGENT_LIFECYCLE_WAKE_REASONS

    sampled = {str(item).strip() for item in active_wake_signal_ids if str(item).strip()}
    return any(
        str(getattr(signal, "root_task_id", "") or "").strip() == selected
        and str(getattr(signal, "reason", "") or "").strip().lower()
        in SUBAGENT_LIFECYCLE_WAKE_REASONS
        and str(getattr(signal, "wake_signal_id", "") or "").strip() not in sampled
        for signal in signals
    )


# LLM: The active mailbox ids are trusted per-turn attributes written by the
# scheduler. Keep the legacy singular id for already persisted/background runs.
# 函数用途: 从当前后台轮属性读取已采样信封编号，兼容旧的单编号字段。
def _active_background_wake_signal_ids(attrs: dict[str, object]) -> tuple[str, ...]:
    raw = attrs.get(CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR)
    values = raw if isinstance(raw, (list, tuple, set)) else ()
    selected = [str(item).strip() for item in values if str(item).strip()]
    legacy = str(attrs.get("background_wake_signal_id") or "").strip()
    if legacy and legacy not in selected:
        selected.insert(0, legacy)
    return tuple(dict.fromkeys(selected))


def _audit_owner_report_pending(store: object, task_id: str) -> bool:
    """Fail closed while an exact Audit owner event still awaits delivery."""

    selected = str(task_id or "").strip()
    if not selected:
        return True
    try:
        wake_signals = store.pending_wake_signals(limit=0)
        observations = store.unhandled_observations_requiring_main(limit=0)
    except Exception:
        return True
    if any(
        str(getattr(signal, "root_task_id", "") or "").strip() == selected
        and str(getattr(signal, "reason", "") or "").strip().lower()
        in {"audit_finding", "audit_capacity_alert"}
        for signal in wake_signals
    ):
        return True
    return any(
        str(getattr(event, "root_task_id", "") or "").strip() == selected
        and str(getattr(event, "event_type", "") or "").strip().lower()
        in {"audit_finding", "audit_capacity_alert"}
        for event in observations
    )


def complete_named_audit_task_if_settled(agent: object, task_id: str) -> bool:
    """Reuse the canonical task terminal gate after host-owned Audit events."""

    selected = str(task_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    loader = getattr(store, "load_task_link", None)
    if not selected or not callable(loader):
        return False
    try:
        link = loader(selected)
    except Exception:
        return False
    if (
        link is None
        or str(getattr(link, "status", "") or "").strip().lower() != "active"
        or str(getattr(link, "work_kind", "") or "").strip().lower() != "audit"
    ):
        return False
    thread_id = str(getattr(link, "thread_id", "") or "").strip()
    if not thread_id:
        return False
    return complete_current_conversation_task(
        agent,
        {
            "conversation_thread_id": thread_id,
            "conversation_task_id": selected,
            "conversation_work_kind": "audit",
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
        },
        source="background_main_agent",
        current_task_id=selected,
    )


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


# LLM: Exact tool-request params are the authority, mirroring 会话运行时 TurnContext propagation.
# The Agent thread-local remains a compatibility fallback only for direct legacy callers.
# 函数用途: 按当前工具请求携带的任务目录重定向旧路径，避免共享 Gateway 并发时读错会话。
def rebase_bound_conversation_workspace_params(
    agent: object,
    value: object,
    *,
    params: object | None = None,
) -> object:
    """Rebase structured tool arguments from this turn's placeholder into the bound task root."""
    current = params if params is not None else getattr(agent, "_current_run_params", None)
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
    # 正式 task root 通常就在占位 owner root 之下，因此重试/归档再走一次
    # 本函数时，必须保护已经以 target 开头的片段，不能叠加第二层目录。
    rebased = _rebase_workspace_text(value, source, target, separator="/")
    return _rebase_workspace_text(rebased, source, target, separator="\\")


# LLM: Rebase may run more than once for the same admitted call; exact target path spans and
# canonical owner tasks/... addresses are protected before replacing source prefixes.  The latter
# protection applies only when the selected target itself proves that source is the owner home.
# 函数用途: 在工具参数中改写启动目录，但保留已经明确指向任一历史任务的绝对地址。
def _rebase_workspace_text(value: str, source: str, target: str, *, separator: str) -> str:
    source_prefix = f"{source}{separator}"
    if source_prefix not in value:
        return value
    target_prefix = f"{target}{separator}"
    pieces: list[str] = []
    cursor = 0
    while True:
        index = value.find(source_prefix, cursor)
        if index < 0:
            pieces.append(value[cursor:])
            break
        pieces.append(value[cursor:index])
        if _workspace_text_starts_with_target(value, index, target, separator=separator):
            pieces.append(target)
            cursor = index + len(target)
            continue
        if _workspace_text_starts_with_owner_task(
            value,
            index,
            source,
            target,
            separator=separator,
        ):
            pieces.append(source_prefix)
            cursor = index + len(source_prefix)
            continue
        pieces.append(target_prefix)
        cursor = index + len(source_prefix)
    return "".join(pieces)


# LLM: An absolute <owner>/tasks[/...] path is an explicit cross-task address, not a placeholder
# cwd.  Infer the owner namespace only from target=<owner>/tasks/...; never protect an arbitrary
# source/tasks child during later task-to-task rebases.
# 函数用途: 识别已经指向用户历史任务目录的绝对路径，避免把它套进当前任务目录。
def _workspace_text_starts_with_owner_task(
    value: str,
    index: int,
    source: str,
    target: str,
    *,
    separator: str,
) -> bool:
    owner_tasks_root = f"{source}{separator}tasks"
    return target.startswith(f"{owner_tasks_root}{separator}") and _workspace_text_starts_with_target(
        value,
        index,
        owner_tasks_root,
        separator=separator,
    )


# LLM: Boundary checking distinguishes the selected target itself/descendants from a merely
# similar path segment such as ``task-1-copy``; punctuation after a path counts as prose boundary.
# 函数用途: 判断当前位置是否已经是正式任务路径，避免错把相似名称当成已转换。
def _workspace_text_starts_with_target(
    value: str,
    index: int,
    target: str,
    *,
    separator: str,
) -> bool:
    if not value.startswith(target, index):
        return False
    end = index + len(target)
    if end >= len(value):
        return True
    next_char = value[end]
    return next_char in {
        separator,
        "/",
        "\\",
        " ",
        "\t",
        "\r",
        "\n",
        '"',
        "'",
        "`",
        ")",
        "]",
        "}",
        ">",
        ",",
        ".",
        ";",
        ":",
        "，",
        "。",
        "；",
        "：",
        "、",
    }


__all__ = [
    "complete_current_conversation_task",
    "complete_named_audit_task_if_settled",
    "conversation_workspace_execution_blocker",
    "conversation_task_execution_state",
    "conversation_thread_execution_state",
    "promote_current_conversation_task",
    "rebase_bound_conversation_workspace_params",
]
