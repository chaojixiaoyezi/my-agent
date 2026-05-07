# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession
    from .manager import SessionManager


# LLM: _channel_activity_items 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理通道activity条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
def _channel_activity_items(cross_channel, user_id: str) -> list[dict]:
    items = []
    for channel in ["chat", "feishu", "qq", "web"]:
        session_ids = cross_channel.list_sessions_by_channel(user_id, channel)
        for session_id in session_ids:
            items.extend(_session_channel_activity(cross_channel, session_id))
    return items


# LLM: _session_channel_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理会话通道activity相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
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


# LLM: _session_update_items 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理会话update条目相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新会话归属、上下文同步和用户隔离，需避免破坏既有状态机约定。
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


# LLM: _task_update_items 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理任务update条目相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新会话归属、上下文同步和用户隔离，需避免破坏既有状态机约定。
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


# LLM: AdminCrossChannelQuery 属于跨通道会话管理的类边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 类用途: 封装管理跨通道通道查询相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发会话归属、上下文同步和用户隔离相关副作用，需保持公开契约稳定。
class AdminCrossChannelQuery:

    ADMIN_USER = "admin"

    # LLM: __init__ 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def __init__(
        self,
        cross_channel: CrossChannelSession,
        session_manager: SessionManager,
        task_registry_store: LocalStore | None = None,
    ):
        self._cross_channel = cross_channel
        self._session_manager = session_manager
        self._task_registry_store = task_registry_store

    # LLM: _check_admin 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 校验管理需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _check_admin(self, user_id: str) -> bool:
        """检查是否为管理员。"""
        return user_id == self.ADMIN_USER

    # LLM: get_all_sessions 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询allsessions需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: get_all_tasks 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询alltasks需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: get_recent_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询recentactivity需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: get_channel_summary 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询通道summary需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: format_admin_summary 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 渲染或汇总管理summary的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def format_admin_summary(self, user_id: str) -> str:
        """获取管理员全局摘要（格式化文本）."""
        if not self._check_admin(user_id):
            return "权限不足：只有 admin 用户可以使用此功能"
        lines = ["=== 管理员全局摘要 ===", ""]
        self._append_channel_summaries(user_id, lines)
        self._append_active_tasks(user_id, lines)
        self._append_recent_activity(user_id, lines)
        return "\n".join(lines)

    # LLM: _append_channel_summaries 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 写入通道summaries的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
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
            _append_summary_sessions(lines, summary)
            lines.append("")

    # LLM: _append_active_tasks 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 写入activetasks的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
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

    # LLM: _append_recent_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 写入recentactivity的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
    def _append_recent_activity(self, user_id: str, lines: list[str]) -> None:
        """Append recent activity timeline."""
        timeline = self.get_recent_activity(user_id, limit=10)
        if not timeline:
            return
        lines.append("## 最近活动")
        for item in timeline[:10]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("timestamp", 0)))
            self._append_activity_line(lines, item, ts)

    # LLM: _append_activity_line 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 写入activityline的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
    def _append_activity_line(self, lines: list[str], item: dict, ts: str) -> None:
        """Format and append a single activity timeline entry."""
        item_type = item["type"]
        formatters = {
            "session_update": _format_session_activity,
            "task_update": _format_task_activity,
            "channel_activity": _format_channel_activity,
        }
        if item_type in formatters:
            lines.append(formatters[item_type](item, ts))


__all__ = ["AdminCrossChannelQuery"]


# LLM: _append_summary_sessions 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 写入sessions的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
def _append_summary_sessions(lines: list[str], summary: dict) -> None:
    sessions = summary.get("sessions")
    if not sessions:
        return
    # LLM: channel summaries cap session details to keep admin output compact.
    lines.append("最近会话:")
    for sess in sessions[:3]:
        lines.append(f"  - {sess['session_id']}: {sess['primary']}")


# LLM: _format_session_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总会话activity的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _format_session_activity(item: dict, ts: str) -> str:
    return f"- [{ts}] 会话 {item['session_id'][:16]}... 在 {item['channel']}"


# LLM: _format_task_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总任务activity的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _format_task_activity(item: dict, ts: str) -> str:
    return f"- [{ts}] 任务 {item['task_id']} -> {item['status']}"


# LLM: _format_channel_activity 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总通道activity的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _format_channel_activity(item: dict, ts: str) -> str:
    active_str = "活跃" if item.get("active") else "非活跃"
    return f"- [{ts}] 通道 {item['channel']} ({active_str})"
