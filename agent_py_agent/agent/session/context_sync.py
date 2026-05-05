"""会话上下文同步。

在通道切换时同步会话上下文。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession


def format_context_for_channel(context: dict, channel: str) -> str:
    """按通道格式化上下文。

    Args:
        context: 上下文字典
        channel: 目标通道（chat/feishu/qq）

    Returns:
        格式化后的上下文文本
    """
    lines = _context_header_lines(context, channel)
    _append_recent_messages(lines, context.get("recent_messages", []), channel)
    _append_pending_reply(lines, context.get("pending_reply"))
    _append_context_tasks(lines, context.get("tasks", []), channel)
    lines.append("请继续对话。")
    return "\n".join(lines)


def _context_header_lines(context: dict, channel: str) -> list[str]:
    lines = ["**会话接续**", ""] if channel == "feishu" else ["[会话接续]"]
    from_channel = context.get("from_channel", "")
    to_channel = context.get("to_channel", "")
    if from_channel and to_channel:
        lines.extend([f"从 {from_channel} 切换到 {to_channel}", ""])
    session_id = context.get("session_id", "")
    if session_id:
        lines.extend([f"原会话 ID: {session_id}", ""])
    return lines


def _append_recent_messages(lines: list[str], recent_messages: list[dict], channel: str) -> None:
    if not recent_messages:
        return
    lines.extend(["**最近上下文:**", ""])
    if channel == "feishu":
        lines.extend(["| 用户 | Agent |", "| --- | --- |"])
        lines.extend(_message_table_rows(recent_messages[-5:]))
    else:
        lines.extend(_message_bullets(recent_messages[-5:]))
    lines.append("")


def _message_table_rows(messages: list[dict]) -> list[str]:
    return [f"| {msg.get('role', 'unknown')} | {msg.get('content', '')[:100]}... |" for msg in messages]


def _message_bullets(messages: list[dict]) -> list[str]:
    return [f"- {msg.get('role', 'unknown')}: {msg.get('content', '')[:100]}..." for msg in messages]


def _append_pending_reply(lines: list[str], pending_reply: object) -> None:
    if pending_reply:
        lines.extend(["**待回复内容:**", str(pending_reply), ""])


def _append_context_tasks(lines: list[str], tasks: list[dict], channel: str) -> None:
    if not tasks:
        return
    lines.extend(["**当前活跃任务:**", ""])
    lines.extend(_format_context_task(task, channel) for task in tasks)
    lines.append("")


def _format_context_task(task: dict, channel: str) -> str:
    task_id = task.get("task_id", "unknown")
    status = task.get("status", "UNKNOWN")
    goal = task.get("goal", "")[:60]
    if channel == "feishu":
        return f"- [{task_id}]({status}): {goal}..."
    return f"- {task_id}: {status} ({goal}...)"


class SessionContextSync:
    """会话上下文同步器。"""

    def __init__(self, cross_channel: CrossChannelSession, task_registry_store: LocalStore | None = None):
        """初始化上下文同步器。

        Args:
            cross_channel: 跨通道会话管理器
            task_registry_store: LocalStore 实例（用于查询任务），可选
        """
        self._cross_channel = cross_channel
        self._task_registry_store = task_registry_store

    def sync_to_channel(self, session_id: str, channel: str) -> dict:
        """将会话上下文同步到目标通道。

        Args:
            session_id: 会话 ID
            channel: 目标通道

        Returns:
            同步后的上下文字典
        """
        # 获取会话信息
        session = self._cross_channel._load_channels(session_id)
        if session is None:
            return {"error": f"会话 {session_id} 不存在"}

        # 构建上下文
        context = {
            "session_id": session_id,
            "user_id": session.get("user_id"),
            "from_channel": session.get("primary_channel"),
            "to_channel": channel,
            "recent_messages": session.get("recent_messages", []),
            "pending_reply": session.get("pending_reply"),
            "tasks": [],
        }

        # 查询关联任务（从 task_registry）
        user_id = session.get("user_id")
        if user_id and self._task_registry_store:
            try:
                from ..task_registry import TaskRegistry
                registry = TaskRegistry(self._task_registry_store)
                # 查询该用户的活跃任务
                active_tasks = registry.query_tasks(user_id=user_id, status="RUNNING", limit=5)
                context["tasks"] = active_tasks
            except Exception:
                pass

        return context

    def update_session_context(
        self,
        session_id: str,
        recent_messages: list[dict] | None = None,
        pending_reply: str | None = None,
    ) -> bool:
        """更新会话的上下文信息。

        Args:
            session_id: 会话 ID
            recent_messages: 最近消息列表
            pending_reply: 待回复内容

        Returns:
            是否成功更新
        """
        data = self._cross_channel._load_channels(session_id)
        if data is None:
            return False

        if recent_messages is not None:
            # 只保留最近 5 条
            data["recent_messages"] = recent_messages[-5:] if len(recent_messages) > 5 else recent_messages

        if pending_reply is not None:
            data["pending_reply"] = pending_reply

        self._cross_channel._save_channels(session_id, data)
        return True


__all__ = ["SessionContextSync", "format_context_for_channel"]
