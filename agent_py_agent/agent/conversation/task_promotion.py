from __future__ import annotations

"""LLM: 任务状态只由结构化工具调用、task link 与 closeout 改变，禁止解析自然语言触发。

模块用途: 在普通会话真的开始工作时提升任务、明确选择旧任务，并在交付完成后关闭候选。
"""

from pathlib import Path


# LLM: 复用已选择的 active link，不得用本轮 prompt 覆盖旧 goal/task_path。
# 函数用途: 任务工具首次执行时把当前 run 绑定到会话，或延续已明确选择的任务。
def promote_current_conversation_task(agent: object, *, goal: str = ""):
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        return None
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(
        attrs.get("conversation_task_id")
        or getattr(current, "task_id", "")
        or getattr(current, "run_id", "")
        or getattr(current, "request_id", "")
        or ""
    ).strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or store is None or not callable(getattr(store, "bind_task", None)):
        return None
    if existing := _active_conversation_link(store, thread_id, task_id):
        attrs["conversation_lane"] = "task"
        attrs["conversation_task_id"] = existing.task_id
        return existing
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
    attrs["conversation_lane"] = "task"
    attrs["conversation_task_id"] = task_id
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


# LLM: 选择只能来自 task_progress action=select 的显式 run_id，不能模糊匹配 goal 文本。
# 函数用途: 将本轮工作切到用户确实要续接的既有任务和工作区；已完成任务会被结构化重新打开。
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
    prior_current_id = str(attrs.get("conversation_task_id") or "").strip()
    link = _reopen_completed_link(store, link)
    if link is None or not _supersede_prior_current(store, thread_id, prior_current_id, selected_id):
        return None
    attrs["conversation_lane"] = "task"
    attrs["conversation_task_id"] = link.task_id
    workspace = _selected_task_workspace(link.task_path)
    if workspace is not None:
        attrs["run_workspace"] = {
            "task_root": str(workspace),
            "output_dir": str(workspace / "output"),
            "work_dir": str(workspace / "work"),
        }
    return link


def _reopen_completed_link(store: object, link: object):
    if str(getattr(link, "status", "") or "").strip().lower() != "completed":
        return link
    try:
        return store.update_task_status({"task_id": link.task_id, "status": "active"})
    except Exception:
        return None


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
    return status in {"active", "completed"} and not task_id.startswith(
        ("subagent-", "bg-main-")
    )


# LLM: 仅由已经通过结构化 closeout 的调用方使用；此函数自身不读取最终回复正文。
# 函数用途: 把完成任务从普通聊天的 active 候选热索引中移除。
def complete_current_conversation_task(
    agent: object,
    task_attributes: object,
    *,
    source: str = "",
    current_task_id: str = "",
) -> bool:
    """在结构化交付收口成功后关闭当前会话任务候选。

    调用方必须先确认交付 closeout 已通过；这里不读取也不猜用户/模型自然语言。
    """
    # 子代理会继承 conversation_task_id，方便它把产物和进度归回父任务；这不等于它
    # 拥有关闭父会话任务的权力。只认结构化 run source，绝不从完成文案猜角色。
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    run_source = str(source or "").strip().lower()
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
    link = _active_conversation_link(store, thread_id, task_id)
    if link is None:
        return False
    try:
        updated = store.update_task_status({"task_id": task_id, "status": "completed"})
    except Exception:
        return False
    if updated is None:
        return False
    attrs["conversation_lane"] = "chat"
    return True


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


__all__ = [
    "complete_current_conversation_task",
    "is_user_selectable_conversation_task",
    "promote_current_conversation_task",
    "select_current_conversation_task",
]
