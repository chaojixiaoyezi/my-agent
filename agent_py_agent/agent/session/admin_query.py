from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession
    from .manager import SessionManager


def _channel_activity_items(cross_channel, user_id: str) -> list[dict]:
    items = []
    for channel in ["chat", "feishu", "qq", "web"]:
        session_ids = cross_channel.list_sessions_by_channel(user_id, channel)
        for session_id in session_ids:
            items.extend(_session_channel_activity(cross_channel, session_id))
    return items


def _session_channel_activity(cross_channel, session_id: str) -> list[dict]:
    items = []
    for ch_info in cross_channel.get_bound_sessions(session_id):
        if ch_info.get("last_active_at", 0) <= 0:
            continue
        items.append(
            {
                "type": "channel_activity",
                "session_id": session_id,
                "channel": ch_info["channel"],
                "active": ch_info["active"],
                "timestamp": ch_info["last_active_at"],
            }
        )
    return items


def _session_update_items(session_manager, user_id: str) -> list[dict]:
    return [
        {
            "type": "session_update",
            "session_id": session.session_id,
            "channel": session.last_active_channel,
            "timestamp": session.updated_at,
        }
        for session in session_manager.list_sessions(user_id=user_id)
    ]


def _task_update_items(task_registry_store, user_id: str) -> list[dict]:
    if not task_registry_store:
        return []
    try:
        from ..task_registry import TaskRegistry
        registry = TaskRegistry(task_registry_store)
        tasks = registry.query_tasks(user_id=user_id, limit=50)
    except Exception:
        return []
    return [
        {
            "type": "task_update",
            "task_id": task["task_id"],
            "status": task["status"],
            "goal": task["goal"],
            "timestamp": task.get("updated_at", 0),
        }
        for task in tasks
    ]


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
        if not self._check_admin(user_id):
            return []

        if not self._task_registry_store:
            return []

        try:
            from ..task_registry import TaskRegistry
            registry = TaskRegistry(self._task_registry_store)
            # admin 的任务
            return registry.query_tasks(user_id=self.ADMIN_USER, status=status, limit=100)
        except Exception:
            return []

    def get_recent_activity(self, user_id: str, limit: int = 20) -> list[dict]:
        if not self._check_admin(user_id):
            return []

        timeline = [
            *_channel_activity_items(self._cross_channel, self.ADMIN_USER),
            *_session_update_items(self._session_manager, self.ADMIN_USER),
            *_task_update_items(self._task_registry_store, self.ADMIN_USER),
        ]
        timeline.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return timeline[:limit]

    def get_channel_summary(self, user_id: str, channel: str) -> dict:
        if not self._check_admin(user_id):
            return {"error": "权限不足"}

        session_ids = self._cross_channel.list_sessions_by_channel(self.ADMIN_USER, channel)
        bound_sessions = []
        for sid in session_ids:
            info = self._cross_channel._load_channels(sid)
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
            except Exception:
                pass

        return {
            "channel": channel,
            "session_count": len(bound_sessions),
            "sessions": bound_sessions,
            "active_task_count": len(active_tasks),
            "active_tasks": active_tasks,
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
            if not summary.get("session_count", 0):
                continue
            lines.extend([
                f"## {channel.upper()} 通道",
                f"会话数: {summary['session_count']}",
                f"活跃任务: {summary['active_task_count']}",
            ])
            if summary.get("sessions"):
                lines.append("最近会话:")
                for sess in summary["sessions"][:3]:
                    lines.append(f"  - {sess['session_id']}: {sess['primary']}")
            lines.append("")

    def _append_active_tasks(self, user_id: str, lines: list[str]) -> None:
        """Append active tasks summary."""
        active_tasks = self.get_all_tasks(user_id, status="RUNNING")
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
        timeline = self.get_recent_activity(user_id, limit=10)
        if not timeline:
            return
        lines.append("## 最近活动")
        for item in timeline[:10]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("timestamp", 0)))
            self._append_activity_line(lines, item, ts)

    def _append_activity_line(self, lines: list[str], item: dict, ts: str) -> None:
        """Format and append a single activity timeline entry."""
        item_type = item["type"]
        if item_type == "session_update":
            lines.append(f"- [{ts}] 会话 {item['session_id'][:16]}... 在 {item['channel']}")
        elif item_type == "task_update":
            lines.append(f"- [{ts}] 任务 {item['task_id']} -> {item['status']}")
        elif item_type == "channel_activity":
            active_str = "活跃" if item.get("active") else "非活跃"
            lines.append(f"- [{ts}] 通道 {item['channel']} ({active_str})")


__all__ = ["AdminCrossChannelQuery"]
