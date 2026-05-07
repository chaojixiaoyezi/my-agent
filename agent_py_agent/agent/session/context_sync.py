# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from .cross_channel import CrossChannelSession


# LLM: format_context_for_channel 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总上下文通道的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def format_context_for_channel(context: dict, channel: str) -> str:
    lines = _context_header_lines(context, channel)
    _append_recent_messages(lines, context.get("recent_messages", []), channel)
    _append_pending_reply(lines, context.get("pending_reply"))
    _append_context_tasks(lines, context.get("tasks", []), channel)
    lines.append("请继续对话。")
    return "\n".join(lines)


# LLM: _context_header_lines 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理上下文headerlines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
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


# LLM: _append_recent_messages 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 写入recent消息的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
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


# LLM: _message_table_rows 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理消息tablerows相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
def _message_table_rows(messages: list[dict]) -> list[str]:
    return [f"| {msg.get('role', 'unknown')} | {msg.get('content', '')[:100]}... |" for msg in messages]


# LLM: _message_bullets 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理消息bullets相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
def _message_bullets(messages: list[dict]) -> list[str]:
    return [f"- {msg.get('role', 'unknown')}: {msg.get('content', '')[:100]}..." for msg in messages]


# LLM: _append_pending_reply 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 写入pendingreply的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
def _append_pending_reply(lines: list[str], pending_reply: object) -> None:
    if pending_reply:
        lines.extend(["**待回复内容:**", str(pending_reply), ""])


# LLM: _append_context_tasks 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 写入上下文tasks的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
def _append_context_tasks(lines: list[str], tasks: list[dict], channel: str) -> None:
    if not tasks:
        return
    lines.extend(["**当前活跃任务:**", ""])
    lines.extend(_format_context_task(task, channel) for task in tasks)
    lines.append("")


# LLM: _format_context_task 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总上下文任务的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _format_context_task(task: dict, channel: str) -> str:
    task_id = task.get("task_id", "unknown")
    status = task.get("status", "UNKNOWN")
    goal = task.get("goal", "")[:60]
    if channel == "feishu":
        return f"- [{task_id}]({status}): {goal}..."
    return f"- {task_id}: {status} ({goal}...)"


# LLM: SessionContextSync 属于跨通道会话管理的类边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 类用途: 封装会话上下文同步相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发会话归属、上下文同步和用户隔离相关副作用，需保持公开契约稳定。
class SessionContextSync:
    """会话上下文同步器。"""

    # LLM: __init__ 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def __init__(self, cross_channel: CrossChannelSession, task_registry_store: LocalStore | None = None):
        self._cross_channel = cross_channel
        self._task_registry_store = task_registry_store

    # LLM: sync_to_channel 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 处理同步to通道相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
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

    # LLM: update_session_context 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 更新会话上下文对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新会话归属、上下文同步和用户隔离，需避免破坏既有状态机约定。
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
