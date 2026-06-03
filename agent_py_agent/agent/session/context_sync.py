
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession


def format_context_for_channel(context: dict, channel: str) -> str:
    lines = _context_header_lines(context, channel)
    _append_recent_messages(lines, context.get("recent_messages", []), channel)
    _append_pending_reply(lines, context.get("pending_reply"))
    _append_context_tasks(lines, context.get("tasks", []), channel)
    _append_context_load_errors(lines, context.get("load_errors", []), channel)
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


def _append_context_load_errors(lines: list[str], load_errors: list[dict], channel: str) -> None:
    if not load_errors:
        return
    header = "**读取警告:**" if channel == "feishu" else "读取警告:"
    lines.extend([header, ""])
    for error in load_errors[:5]:
        context = error.get("context", "unknown")
        message = error.get("model_message") or error.get("message") or "读取失败"
        lines.append(f"- {context}: {message}")
    lines.append("")


class SessionContextSync:
    """会话上下文同步器。"""

    def __init__(self, cross_channel: CrossChannelSession, task_registry_store: LocalStore | None = None):
        self._cross_channel = cross_channel
        self._task_registry_store = task_registry_store

    def sync_to_channel(self, session_id: str, channel: str) -> dict:
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
            "load_errors": [],
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
            except Exception as exc:
                context["load_errors"].append(
                    runtime_error_report(exc, context="session.context_sync.tasks")
                )

        return context

    def update_session_context(
        self,
        session_id: str,
        recent_messages: list[dict] | None = None,
        pending_reply: str | None = None,
    ) -> bool:
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
