
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .admin_query_helpers import (
    admin_load_error,
    append_admin_load_errors,
    append_summary_sessions,
    channel_activity_items_report,
    format_channel_activity,
    format_session_activity,
    format_task_activity,
    session_update_items,
    task_update_items_report,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession
    from .manager import SessionManager


class AdminCrossChannelQuery:

    ADMIN_USER = "admin"

    def __init__(
        self,
        cross_channel: CrossChannelSession,
        session_manager: SessionManager,
        task_registry_store: LocalStore | None = None,
    ):
        self._cross_channel = cross_channel
        self._session_manager = session_manager
        self._task_registry_store = task_registry_store

    def _check_admin(self, user_id: str) -> bool:
        """检查是否为管理员。"""
        return user_id == self.ADMIN_USER

    def get_all_sessions(self, user_id: str) -> list[dict]:
        if not self._check_admin(user_id):
            return []

        # 从 session_manager 获取所有会话
        sessions = self._session_manager.list_sessions(user_id=None)  # 获取所有会话

        result = []
        for session in sessions:
            # 检查是否是 admin 的会话
            if session.user_id != self.ADMIN_USER:
                continue

            # 获取通道绑定信息
            bound_channels = self._cross_channel.get_bound_sessions(session.session_id)
            primary = self._cross_channel.get_primary_channel(session.session_id)

            result.append({
                "session_id": session.session_id,
                "user_id": session.user_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_active_channel": session.last_active_channel,
                "primary_channel": primary,
                "bound_channels": bound_channels,
            })

        return result

    def get_all_tasks(self, user_id: str, status: str | None = None) -> list[dict]:
        tasks, _load_errors = self.get_all_tasks_report(user_id, status=status)
        return tasks

    def get_all_tasks_report(self, user_id: str, status: str | None = None) -> tuple[list[dict], list[dict]]:
        if not self._check_admin(user_id):
            return [], []

        if not self._task_registry_store:
            return [], []

        try:
            from ..task_registry import TaskRegistry
            registry = TaskRegistry(self._task_registry_store)
            # admin 的任务
            return registry.query_tasks(user_id=self.ADMIN_USER, status=status, limit=100), []
        except Exception as exc:
            return [], [admin_load_error(exc, context="session.admin.tasks")]

    def get_recent_activity(self, user_id: str, limit: int = 20) -> list[dict]:
        timeline, _load_errors = self.get_recent_activity_report(user_id, limit=limit)
        return timeline

    def get_recent_activity_report(self, user_id: str, limit: int = 20) -> tuple[list[dict], list[dict]]:
        if not self._check_admin(user_id):
            return [], []

        channel_items, channel_errors = channel_activity_items_report(self._cross_channel, self.ADMIN_USER)
        task_items, task_errors = task_update_items_report(
            self._task_registry_store,
            self.ADMIN_USER,
            context="session.admin.recent_activity.tasks",
        )
        timeline = [
            *channel_items,
            *session_update_items(self._session_manager, self.ADMIN_USER),
            *task_items,
        ]
        timeline.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return timeline[:limit], [*channel_errors, *task_errors]

    def get_channel_summary(self, user_id: str, channel: str) -> dict:
        if not self._check_admin(user_id):
            return {"error": "权限不足"}

        session_ids, load_errors = self._cross_channel.list_sessions_by_channel_report(self.ADMIN_USER, channel)
        bound_sessions = []
        for sid in session_ids:
            info, load_error = self._cross_channel._load_channels_report(sid)
            if load_error is not None:
                load_errors.append(load_error)
                continue
            if info:
                bound_sessions.append({
                    "session_id": sid,
                    "primary": self._cross_channel.get_primary_channel(sid),
                    "channels": info.get("channels", {}),
                    "recent_messages": info.get("recent_messages", [])[-3:] if info.get("recent_messages") else [],
                })

        # 获取该通道活跃的任务
        active_tasks = []
        if self._task_registry_store:
            try:
                from ..task_registry import TaskRegistry
                registry = TaskRegistry(self._task_registry_store)
                active_tasks = registry.query_tasks(user_id=self.ADMIN_USER, status="RUNNING", limit=10)
            except Exception as exc:
                load_errors.append(
                    admin_load_error(exc, context="session.admin.channel_summary.tasks")
                )

        return {
            "channel": channel,
            "session_count": len(bound_sessions),
            "sessions": bound_sessions,
            "active_task_count": len(active_tasks),
            "active_tasks": active_tasks,
            "load_errors": load_errors,
        }

    def format_admin_summary(self, user_id: str) -> str:
        """获取管理员全局摘要（格式化文本）."""
        if not self._check_admin(user_id):
            return "权限不足：只有 admin 用户可以使用此功能"
        lines = ["=== 管理员全局摘要 ===", ""]
        self._append_channel_summaries(user_id, lines)
        self._append_active_tasks(user_id, lines)
        self._append_recent_activity(user_id, lines)
        return "\n".join(lines)

    def _append_channel_summaries(self, user_id: str, lines: list[str]) -> None:
        """Append per-channel summaries to lines list."""
        for channel in ["chat", "feishu", "qq", "web"]:
            summary = self.get_channel_summary(user_id, channel)
            append_admin_load_errors(lines, summary.get("load_errors", []))
            if not summary.get("session_count", 0):
                continue
            lines.extend([
                f"## {channel.upper()} 通道",
                f"会话数: {summary['session_count']}",
                f"活跃任务: {summary['active_task_count']}",
            ])
            append_summary_sessions(lines, summary)
            lines.append("")

    def _append_active_tasks(self, user_id: str, lines: list[str]) -> None:
        """Append active tasks summary."""
        active_tasks, load_errors = self.get_all_tasks_report(user_id, status="RUNNING")
        append_admin_load_errors(lines, load_errors)
        if not active_tasks:
            return
        lines.append(f"## 活跃任务 ({len(active_tasks)} 个)")
        for task in active_tasks[:10]:
            lines.append(f"- [{task['task_id']}] {task['goal'][:60]}...")
        if len(active_tasks) > 10:
            lines.append(f"  ... 还有 {len(active_tasks) - 10} 个任务")
        lines.append("")

    def _append_recent_activity(self, user_id: str, lines: list[str]) -> None:
        """Append recent activity timeline."""
        timeline, load_errors = self.get_recent_activity_report(user_id, limit=10)
        append_admin_load_errors(lines, load_errors)
        if not timeline:
            return
        lines.append("## 最近活动")
        for item in timeline[:10]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("timestamp", 0)))
            self._append_activity_line(lines, item, ts)

    def _append_activity_line(self, lines: list[str], item: dict, ts: str) -> None:
        """Format and append a single activity timeline entry."""
        item_type = item["type"]
        formatters = {
            "session_update": format_session_activity,
            "task_update": format_task_activity,
            "channel_activity": format_channel_activity,
        }
        if item_type in formatters:
            lines.append(formatters[item_type](item, ts))


__all__ = ["AdminCrossChannelQuery"]
