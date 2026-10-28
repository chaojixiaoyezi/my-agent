from __future__ import annotations

"""LLM: 运行状态只由结构化结果、task link 与子代理事件改变；文件路径不再触发任务选择或参数重写。

模块用途: 建立可恢复运行身份并有界解析持久继任链；切走目录先中断旧代，真正换代才发表替代边，目录权限独立。
"""

import hashlib
import time
from pathlib import Path

from .authority import (
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


# LLM: 原活动回合锁先于任务锁，覆盖晋升与本轮参数同步；不持锁调用模型，停止时沿原 InterruptedError 退出。
# 函数用途: 为首个工作工具和能力入口准备提供同一任务晋升事务，防止停止后重新建立活动任务。
def promote_conversation_task_for_run(agent: object, params: object):
    # LLM: 只从外层 RunParams 同步到本轮投影，不接受模型内容或反向修改权威身份。
    # 函数用途: 在同一活动回合事务内登记任务并刷新循环参数中的绑定。
    def promote():
        link = promote_current_conversation_task(agent)
        authoritative = getattr(getattr(agent, "_current_run_params", None), "task_attributes", None)
        projection = getattr(params, "task_attributes", None)
        if link is not None and isinstance(authoritative, dict) and isinstance(projection, dict) and projection is not authoritative:
            projection.clear()
            projection.update(authoritative)
        return link

    transition = getattr(params, "active_turn_transition_callback", None)
    return transition("task_promotion", promote) if callable(transition) else promote()


# LLM: A thread's latest workspace projection is not a live-task capability. The first work tool
# binds a fresh request unless exact request/Goal/active authority already selected a workspace.
# 函数用途: 本轮工作时建立运行身份；精确旧根沿继任链续接，坏链失败关闭，不回落新任务。
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
    if not thread_id or not task_id or store is None or not callable(getattr(getattr(store, 'tasks', None), 'bind', None)):
        return None
    from ..runtime_context import current_subagent_run_id

    child_run_id = current_subagent_run_id(agent)
    if attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is True and not child_run_id:
        loader = getattr(getattr(store, 'tasks', None), 'load', None)
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
            if _superseded_workspace_selection_failed(store, candidate):
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
    bind_options = _promotion_bind_options(agent, current, thread_id)
    try:
        link = store.tasks.bind(
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
            },
            **bind_options,
        )
    except Exception:
        return None
    # task_attributes 是本轮结构化状态；后续派工、wait、workspace writer 都从这里继承，
    # 不再从用户自然语言猜“是不是任务”。
    attrs["conversation_task_id"] = task_id
    selected = _materialize_promoted_workspace(agent, current, link, task_goal=task_goal) or link
    return _activate_current_conversation_task(store, current, attrs, selected)


# LLM: 能力选择/宿主身份/原输入各用typed keyword，不读模型属性；委派引用保留在原SessionTask，TaskLink只指向它。
# 函数用途: 为原TaskStore.bind准备宿主选项，冻结原根输入位置，不建立第二引用或身份账本。
def _promotion_bind_options(agent, current, thread_id):
    from ..capability.package_selection_scope import new_task_capability_selection
    from .host_actor import resolve_host_actor
    from .host_message_context import promotion_context_binding

    selection = new_task_capability_selection(agent, current)
    actor = resolve_host_actor(agent, thread_id) if getattr(current, "host_channel_actor", None) is not None else None
    context = promotion_context_binding(agent, current, actor, thread_id)
    return {**({"capability_selection": selection} if selection is not None else {}),
        **({"host_channel_actor": actor} if actor is not None else {}),
        **({"host_message_context": context} if context is not None else {})}


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


# LLM: 运行归档仅由通用 writer 建立；会话层同步 task link，不能更改用户 cwd 或文件访问范围。
# 函数用途: 创建或恢复本轮宿主记录，保留主代理和子代理用于停止、进度与恢复的唯一身份。
def _materialize_promoted_workspace(
    agent: object,
    current: object,
    link: object,
    *,
    task_goal: str,
):
    """把已晋升会话任务绑定到真实 workspace；失败时保留任务链接但不伪造路径。"""
    attrs = getattr(current, "task_attributes", None)
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
        )
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return link
    try:
        return store.tasks.bind(
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
        links, errors = store.tasks.active_report(thread_id)
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
    loader = getattr(getattr(store, 'threads', None), 'load', None)
    if not callable(loader):
        return ""
    try:
        thread = loader(thread_id)
    except Exception:
        return ""
    if thread is None:
        return ""
    return str(getattr(thread, "workspace_task_id", "") or "").strip()


# LLM: 先沿有界继任链解析当前link再核其执行权；换代编号锚定原选择ID，重复绑定不自我supersede，旧终态不复活。
# 函数用途: 按同 owner/thread 的明确运行身份恢复归档引用；不因访问旧文件而调用。
def bind_current_conversation_workspace(agent: object, task_id: str):
    """由结构化会话身份绑定既有运行归档，不读取工具文件路径，也不作为模型工具暴露。"""
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
    link = _activate_reusable_workspace_link(agent, store, link, preferred_id=selected_id)
    if link is None or not _supersede_prior_current(store, thread_id, prior_current_id, link.task_id):
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
    if not _publish_current_request_task_binding(current, link):
        return None
    if _remove_rebound_placeholder_workspace(
        agent,
        store,
        thread_id=thread_id,
        task_id=prior_current_id,
        source_workspace=source_workspace,
        target_workspace=str(workspace or ""),
    ):
        attrs["conversation_placeholder_workspace_cleanup"] = {
            "status": "removed",
            "task_id": prior_current_id,
        }
    return link


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


# LLM: Cleanup follows—not precedes—the exact successor bind, sticky selection, request-binding
# publish, and prior-link supersede. It delegates deletion to the untouched-scaffold verifier;
# user files, unknown workspace layouts, active links, and cross-owner paths always survive.
# 函数用途: 精确回到旧任务后移除本轮自动生成但尚未承载用户内容的占位目录，避免任务列表积累空壳。
def _remove_rebound_placeholder_workspace(
    agent: object,
    store: object,
    *,
    thread_id: str,
    task_id: str,
    source_workspace: str,
    target_workspace: str,
) -> bool:
    source = _selected_task_workspace(source_workspace)
    target = _selected_task_workspace(target_workspace)
    candidate_id = str(task_id or "").strip()
    if source is None or target is None or source == target or not candidate_id:
        return False
    try:
        link = store.tasks.load(candidate_id)
        thread = store.threads.load(thread_id)
    except Exception:
        return False
    if (
        link is None
        or thread is None
        or str(getattr(link, "thread_id", "") or "").strip() != thread_id
        or str(getattr(link, "status", "") or "").strip().lower() != "superseded"
        or _selected_task_workspace(getattr(link, "task_path", "")) != source
    ):
        return False
    from ..user_space.run_workspace import remove_unmodified_run_workspace

    return remove_unmodified_run_workspace(
        source,
        owner_home=str(getattr(thread, "owner_home", "") or ""),
        task_id=candidate_id,
    )




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
    writer = getattr(getattr(store, 'tasks', None), 'select_workspace_task', None)
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


# LLM: run_workspace 只承载宿主运行账本和恢复引用，不是用户文件的 cwd 或权限根。
# 函数用途: 切换当前运行记录的归档位置；家目录、显式工具路径和会话 cwd 保持原样。
def _set_current_task_workspace(
    agent: object,
    attrs: dict[str, object],
    workspace: Path,
) -> None:
    attrs["run_workspace"] = {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }
    agent._current_run_task_workspace = str(workspace)


# LLM: active Goal才可原位续接；普通终态沿原选择ID分配新代次，暂停/阻塞目标不能借工作工具激活。
# 函数用途: 激活有执行授权的既有目录；普通已结束工作建立新代次，暂停目标不变。
def _activate_reusable_workspace_link(agent: object, store: object, link: object, *, preferred_id: str = ""):
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
        return _continue_terminal_link_as_new_execution(agent, store, link, preferred_id=preferred_id)
    try:
        reopened = store.tasks.update_status({"task_id": link.task_id, "status": "active"})
    except Exception:
        return None
    if reopened is None:
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


# LLM: 三态分别表示 active Goal 原位续接、普通新代次、不可自动续接；不写 Goal 状态。
# 函数用途: 检查精确目标是否允许恢复这份任务，不把用户新聊天当作 resume。
def _bound_goal_requires_in_place_resume(store: object, link: object) -> bool | None:
    try:
        goal = store.goals.load(
            str(getattr(link, "thread_id", "") or ""),
            task_id=str(getattr(link, "task_id", "") or ""),
        )
    except Exception:
        return None
    if goal is None or str(getattr(goal, "task_id", "") or "") != str(
        getattr(link, "task_id", "") or ""
    ):
        return False
    status = str(getattr(goal, "status", "") or "").strip().lower()
    if status == "active":
        return True
    return False if status == "complete" else None


# LLM: 后继transition锁覆盖存在性快照、bind和指针发布；失败先核幂等提交，再回收本次新建且未变化的孤儿。
# 函数用途: 为同目录续作建立稳定新执行代，发布失败时不留下未挂接的活动任务。
def _continue_terminal_link_as_new_execution(agent: object, store: object, link: object, *, preferred_id: str = ""):
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
        source_id=preferred_id or source_id,
        task_path=str(getattr(link, "task_path", "") or ""),
    )
    if not successor_id:
        return None
    # A terminal link selected by exact authority contributes its canonical cwd. The successor
    # is a new 会话运行时 active turn, so its objective must come from this turn's exact
    # user input rather than the historical execution that previously occupied
    # the directory. 后台无新输入时才继承精确旧目标，普通前台仍不能借空提示续接旧工作。
    current_goal = next((text for key in ('root_user_prompt', 'user_prompt', 'prompt')
                         if (text := str(getattr(current, key, '') or '').strip())), '')
    if not current_goal and getattr(current, 'source', '') == 'background_main_agent':
        current_goal = str(getattr(link, 'goal', '') or '').strip()
    if not current_goal:
        return None
    from ..capability.package_selection_scope import new_task_capability_selection

    selection = new_task_capability_selection(agent, current)
    try:
        successor = _bind_and_publish_successor(store, link, {
            'thread_id': thread_id, 'task_id': successor_id, 'goal': current_goal,
            'status': 'active', 'task_path': str(getattr(link, 'task_path', '') or ''),
        }, selection=selection)
    except Exception:
        return None
    if successor is None:
        return None
    attrs["conversation_continued_from_task_id"] = preferred_id or source_id
    return successor


# LLM: 后继transition锁覆盖创建与发布；canonical文件内先原子确认创建归属，bind部分提交异常也有准确初始快照可补偿。
# 函数用途: 绑定新执行代并发布继任边，防止存在性检查竞态误删他人任务。
def _bind_and_publish_successor(store, previous, request, *, selection):
    with store.tasks.transition_guard(request['task_id']):
        initial = store.tasks.load(request['task_id'])
        created_new = initial is None
        prior_workspace_id = store.threads.require(request['thread_id']).workspace_task_id
        if created_new:
            initial, created_new = _reserve_successor_creation(store, request, selection=selection)
        try:
            successor = store.tasks.bind(request,
                **({'capability_selection': selection} if selection is not None else {}))
        except Exception:
            _publish_or_rollback_successor(store, previous, initial, created_new=created_new,
                prior_workspace_id=prior_workspace_id, publish=False)
            return None
        published = _publish_or_rollback_successor(store, previous, successor,
            created_new=created_new, prior_workspace_id=prior_workspace_id)
        return successor if published else None


# LLM: 复用TaskStore原合并函数和canonical JSON更新器，不另造schema；只有锁内原文件不存在才取得本次创建资格。
# 函数用途: 原子保存可补偿的初始任务快照，并区分并发抢先创建的既有记录。
def _reserve_successor_creation(store, request, *, selection):
    from ..gateway_parts.io import update_json_file_atomic
    from .models import ThreadTaskLink
    from .store_tasks import _merged_task_link

    path, created = store.storage.task_path(request['task_id']), False

    # LLM: 已有记录原样保留；空但存在的坏文件沿原合并器报错，不能覆盖成新任务。
    # 函数用途: 在原JSON锁内建立本次新任务，冻结创建归属而不是依赖锁外load快照。
    def reserve(data):
        nonlocal created
        if data:
            return data
        initial = _merged_task_link(data, request=request, thread_id=request['thread_id'],
            task_id=request['task_id'], current=time.time(), path_exists=path.exists(), capability_selection=selection)
        created = True
        return initial.to_dict()

    payload = update_json_file_atomic(path, reserve)
    return ThreadTaskLink.from_dict(payload), created


# LLM: 调用方已持后继transition锁；失败后在前代锁内重读有效继任边，提交已发生即幂等成功，不误删并发成功任务。
# 函数用途: 区分指针未发表和回执丢失，仅未挂接且本次新建的任务进入回滚。
def _publish_or_rollback_successor(store, previous, successor, *, created_new, prior_workspace_id, publish=True):
    try:
        if publish and _persist_superseded_successor(store, previous, successor):
            return True
    except Exception:
        pass
    with store.tasks.transition_guard(previous.task_id):
        latest = store.tasks.load(previous.task_id)
        if (latest is not None and latest.task_id == previous.task_id
                and latest.thread_id == successor.thread_id and latest.status == 'superseded'
                and latest.superseded_by_task_id == successor.task_id
                and _selected_task_workspace(latest.task_path) == _selected_task_workspace(successor.task_path)):
            # 指针先落盘而索引修复失败时，仍由原TaskStore收尾，不能把已提交继任当孤儿删除。
            try:
                store.tasks.update_status({'task_id': latest.task_id, 'status': 'superseded',
                    'expected_status': 'superseded'})
            except Exception:
                pass
            return True
        if created_new:
            _rollback_unpublished_successor(store, successor, prior_workspace_id=prior_workspace_id)
    return False


# LLM: 前代/后继transition锁由调用方持有；canonical任务JSON锁覆盖全值比较和删除；删除失败先退役并保留可重试历史。
# 函数用途: 回收本次新建且仍为初始active的未挂接任务，删除自身失败也不得留下活动孤儿。
def _rollback_unpublished_successor(store, successor, *, prior_workspace_id):
    import json
    from dataclasses import replace

    from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
    from ..common.nofollow_fs import read_bytes_beneath, unlink_file_beneath

    path = store.storage.task_path(successor.task_id)
    parts = path.relative_to(store.storage.root).parts

    # LLM: 仅移除本次任务的索引；其它任务和并发工作区选择均保留，只有仍指向待删任务时恢复原显示指针。
    # 函数用途: 在原线程锁内合并回滚成员关系，避免用旧线程快照覆盖并发更新。
    def restore_thread(thread):
        return replace(thread,
            task_ids=tuple(item for item in thread.task_ids if item != successor.task_id),
            active_task_ids=tuple(item for item in thread.active_task_ids if item != successor.task_id),
            workspace_task_id=prior_workspace_id if thread.workspace_task_id == successor.task_id else thread.workspace_task_id)

    with locked_json_path(path):
        raw = read_bytes_beneath(store.storage.root, parts, max_bytes=16 * 1024 * 1024)
        if successor.status != 'active' or raw is None:
            return
        if json.loads(raw) != json.loads(json.dumps(successor.to_dict(), ensure_ascii=False)):
            return
        store.threads.update_atomic(successor.thread_id, restore_thread)
        try:
            unlink_file_beneath(store.storage.root, parts)
        except OSError:
            # 文件删除失败时保留原任务的终态历史，下一次稳定编号可跳过它，不能留下未索引的active记录。
            retired = {**json.loads(raw), 'status': 'superseded'}
            write_private_text_file_atomic_unlocked(path, json.dumps(retired, ensure_ascii=False, sort_keys=True))
            store.tasks.update_thread_index(successor.thread_id, successor.task_id, 'superseded', time.time())


# LLM: 使用同一canonical任务文件与原transition/JSON锁，复核线程、路径、终态及既有指针，禁止迟到覆写新继任。
# 函数用途: 原子保存前代的superseded状态和继任ID，再由TaskStore修复索引及终态副作用。
def _persist_superseded_successor(store, previous, successor) -> bool:
    from ..gateway_parts.io import update_json_file_atomic
    from .models import ThreadTaskLink

    # LLM: JSON锁内校验最新记录而非用旧快照覆盖；幂等重试保留原JSON和全部未知字段。
    # 函数用途: 为这次同线程同路径换代发布唯一继任边。
    def update(data):
        latest = ThreadTaskLink.from_dict(data)
        if (latest.task_id != previous.task_id or latest.thread_id != successor.thread_id
                or _selected_task_workspace(latest.task_path) != _selected_task_workspace(successor.task_path)):
            raise ValueError('TASK_SUCCESSOR_BINDING_INVALID')
        if latest.status == 'superseded' and latest.superseded_by_task_id == successor.task_id:
            return data
        if latest.status not in {'completed', 'interrupted'} or latest.superseded_by_task_id:
            raise ValueError('TASK_SUCCESSOR_CONFLICT')
        return {**data, 'status': 'superseded', 'superseded_by_task_id': successor.task_id}

    try:
        with store.tasks.transition_guard(previous.task_id):
            update_json_file_atomic(store.storage.task_path(previous.task_id), update, require_existing=True)
            retired = store.tasks.update_status({'task_id': previous.task_id, 'status': 'superseded',
                                                 'expected_status': 'superseded'})
        return retired is not None
    except Exception:
        return False


# LLM: 同路径终态base可跳过已结束前代；已退休请求的跨目录往返可跨interrupted代，active跨路径仍只跳superseded。
# 函数用途: 为多次后台唤醒分配后继编号，同时保留同请求目录往返和失败关闭边界。
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
        links, errors = store.tasks.list_report(thread_id)
    except Exception:
        return ""
    if errors:
        return ""
    by_id = {
        str(getattr(item, "task_id", "") or ""): item
        for item in links
        if str(getattr(item, "task_id", "") or "")
    }
    base_existing = by_id.get(base_id)
    if base_existing is None:
        return base_id
    base_status = str(getattr(base_existing, 'status', '') or '').strip().lower()
    base_same_path = str(getattr(base_existing, 'task_path', '') or '') == task_path
    if base_same_path and base_status == 'active':
        return base_id
    allowed_prior_statuses = ({'superseded', 'completed', 'interrupted'}
                             if base_same_path and base_status in {'superseded', 'completed', 'interrupted'}
                             else {'superseded'})
    if base_status == 'superseded':
        allowed_prior_statuses.add('interrupted')
    suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
    derived = f"{base_id}-continue-{suffix}"
    # 已占用编号数不超过任务数，因此这段有界扫描必能发现空槽或明确的状态/路径冲突。
    # 同一请求切走的真实目录以interrupted结束，返回后再原子发表该代的继任边；不跳过失败/取消或未知状态。
    for visit in range(len(by_id) + 1):
        candidate = derived if visit == 0 else f"{derived}-visit-{visit + 1}"
        existing = by_id.get(candidate)
        if existing is None:
            return candidate
        if str(getattr(existing, "task_path", "") or "") != task_path:
            return ""
        status = str(getattr(existing, "status", "") or "").strip()
        if status == "active":
            return candidate
        if status not in allowed_prior_statuses:
            return ""
    return ""


# LLM: 尚无同路径继任者时不能写superseded断链；真实目录切走记interrupted，占位任务无目录可直接退休。
# 函数用途: 结束当前请求上一工作区的活动代，不伪造完成或跨路径继任边，不改变历史目录。
def _supersede_prior_current(
    store: object,
    thread_id: str,
    prior_current_id: str,
    selected_id: str,
) -> bool:
    if not prior_current_id or prior_current_id == selected_id:
        return True
    prior = _active_conversation_link(store, thread_id, prior_current_id)
    if prior is None:
        return True
    try:
        return store.tasks.update_status(
            {"task_id": prior_current_id, "status": "interrupted" if prior.task_path else "superseded"}
        ) is not None
    except Exception:
        return False


# LLM: 精确选择优先，在同线程快照内识别原根；superseded只可沿持久指针解析，不能按命名或最新任务猜继任。
# 函数用途: 从认证会话的任务关联中解析可复用当前代次。
def _reusable_conversation_workspace_link(store: object, thread_id: str, task_id: str):
    try:
        links, errors = store.tasks.list_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    link = next((item for item in links if item.task_id == task_id and item.thread_id == thread_id), None)
    if link is not None and link.status == 'superseded':
        return _resolve_superseded_successor_link(store, links, link, thread_id=thread_id)
    return link if link is not None and is_reusable_conversation_workspace(link) else None


# LLM: 最多16跳，每条边均须存在于同线程快照且重新读取权威记录；环、缺边、跨路径和不安全状态立即拒绝。
# 函数用途: 沿持久继任链恢复原根当前代次，不授予新工作区或新执行权。
def _resolve_superseded_successor_link(store, links, link, *, thread_id):
    workspace = _selected_task_workspace(link.task_path)
    if workspace is None or not workspace.is_dir() or link.task_id.lower().startswith(('subagent-', 'bg-main-')):
        return None
    by_id, visited = {item.task_id: item for item in links}, {link.task_id}
    for _ in range(16):
        next_id = getattr(link, 'superseded_by_task_id', '')
        if not isinstance(next_id, str) or not next_id or next_id in visited or next_id not in by_id:
            return None
        visited.add(next_id)
        try:
            link = store.tasks.load(next_id)
        except Exception:
            return None
        if (link is None or link.task_id != next_id or link.thread_id != thread_id
                or _selected_task_workspace(link.task_path) != workspace):
            return None
        if link.status != 'superseded':
            return link if is_reusable_conversation_workspace(link) else None
    return None


# LLM: 只有原选择确属superseded或读取失效时阻止新任务回落；不改变普通新提示的原有选择规则。
# 函数用途: 防止坏继任链被非空提示绕过为影子执行。
def _superseded_workspace_selection_failed(store, task_id) -> bool:
    try:
        link = store.tasks.load(task_id)
        return link is not None and link.status == 'superseded'
    except Exception:
        return True


def is_reusable_conversation_workspace(link: object) -> bool:
    task_id = str(getattr(link, "task_id", "") or "").strip().lower()
    status = str(getattr(link, "status", "") or "").strip().lower()
    return status in {"active", "completed", "interrupted"} and not task_id.startswith(
        ("subagent-", "bg-main-")
    )


# LLM: 原根选择可能已换代；执行状态必须按解析出的当前代次检查，不能漏掉继任代的活跃策略或claim。
# 函数用途: 阻止对正在执行的精确任务或其当前继任代创建第二执行者。
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
    state = conversation_task_execution_state(store, thread_id, link.task_id)
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


# LLM: 运行观测只使用同 thread 的 typed policy/watch_run_id，不把等待者自身当成活跃执行。
# 函数用途: 汇总持久进度策略所观察的运行身份，供恢复与重复执行检查使用，不判断业务目录访问权。
def _append_thread_policy_execution_state(
    store: object,
    thread_id: str,
    sources_by_task_id: dict[str, list[str]],
    load_errors: list[dict[str, object]],
) -> None:
    loader = getattr(getattr(store, 'progress', None), 'list_report', None)
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
            # wait 的 task_id 是等待者的调度归属，watch_run_id 才是被观察对象。
            # 将父任务自己的等待策略算作运行证据，会错误阻止其 typed wake 续接。
            # self-watch（watch_run_id 空或等于 task_id）不证明任何 run 在执行。
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
    loader = getattr(getattr(store, "claims", None), "load_report", None)
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


# LLM: 插话持久操作经 guidance 领域组件； 仅由正常 runtime turn 终态调用；当前子树和未读信封是权威，不用工作片开始时的冻结阶段否定已接收结果。
# 函数用途: 在没有待处理引导、活跃目标、未终态子代理或未读事件时关闭普通会话任务，不检查回复质量。
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
    completed_work_kind = ""
    audit_activation_ready = True
    try:
        with store.tasks.transition_guard(task_id):
            # `/goal` 的每一轮也会正常结束当前模型 turn，但“本轮有最终回复”不等于
            # “整个持续目标已经达成”。活跃目标只能由 update_goal 明确写入终态；
            # 目标记录损坏时同样 fail-closed，不能趁读取失败误关根任务。
            goal = store.goals.load(thread_id, task_id=task_id)
            if goal is not None and goal.task_id == task_id and goal.status != "complete":
                return False
            link = _active_conversation_link(store, thread_id, task_id)
            if (
                link is None
                or store.guidance.pending("task", task_id, limit=1)
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
            updated = store.tasks.update_status(
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
    loader = getattr(getattr(store, 'wakes', None), 'pending_report', None)
    try:
        if callable(loader):
            signals, load_errors = loader(limit=0)
            if load_errors:
                return True
        else:
            signals = store.wakes.pending(limit=0)
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
        wake_signals = store.wakes.pending(limit=0)
        observations = store.observations.unhandled_requiring_main(limit=0)
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
    loader = getattr(getattr(store, 'tasks', None), 'load', None)
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
        return conversation_task_open_subagents_or_raise(agent, task_id)
    except Exception:
        return True


# LLM: 与 _conversation_task_has_open_subagents 同一判据，但读取失败原样抛出，供需要区分"读不到"和"有子代理"的
#   调用方（conversation.task_follow_up）记录结构化告警；不能在这里吞异常。
# 函数用途: 判断根任务是否仍有未终态子代理，读取失败时抛出。
def conversation_task_open_subagents_or_raise(agent: object, task_id: str) -> bool:
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

    return any(
        not task_status_in(getattr(run, "status", ""), SUBAGENT_ENDED_STATUSES)
        for run in conversation_task_lineage_runs_or_raise(agent, task_id)
    )


# LLM: 血缘 id 仍按 subagent_run_ids_for_request 选，记录按 id 精确读（list_runs_by_ids_report）；血缘里有、记录却
#   缺失或读不出的，抛 SubagentLineageUnreadable（带第一条结构化错误报告），调用方不能把它当成"子代理还在跑"或"没有"。
# 函数用途: 读取一个会话任务血缘里的全部子代理记录，缺失或损坏时抛出。
def conversation_task_lineage_runs_or_raise(agent: object, task_id: str) -> list[object]:
    run_ids = sorted(set(agent.subagent_run_ids_for_request(task_id)))
    if not run_ids:
        return []
    report = agent.subagents.list_runs_by_ids_report(run_ids)
    if report.load_errors:
        raise SubagentLineageUnreadable(dict(report.load_errors[0]))
    return list(report.runs)


# LLM: report 是子代理持久层的结构化读取错误（category/error_type/run_id/path），不含记录正文。
# 类用途: 会话任务血缘里有子代理记录缺失或读不出。
class SubagentLineageUnreadable(RuntimeError):
    # 函数用途: 保存结构化错误报告。
    def __init__(self, report: dict[str, object]) -> None:
        super().__init__("subagent lineage record unreadable")
        self.report = report


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




__all__ = [
    "complete_current_conversation_task",
    "complete_named_audit_task_if_settled",
    "conversation_task_execution_state",
    "conversation_thread_execution_state",
    "promote_current_conversation_task",
]
