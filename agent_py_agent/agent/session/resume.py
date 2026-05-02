"""会话恢复模块。

提供从会话 ID 恢复历史上下文的功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent

from .manager import SessionManager


def resume_session(agent: SimpleAgent, session_id: str) -> dict:
    """恢复会话上下文。

    从会话的历史记忆和任务上下文中恢复信息。

    Args:
        agent: SimpleAgent 实例
        session_id: 会话 ID

    Returns:
        包含恢复上下文的字典，包括：
        - session: Session 对象
        - recent_memories: 最近记忆列表
        - subagent_context: 子代理上下文
    """
    manager = SessionManager(agent.config)
    session = manager.load_session(session_id)

    if session is None:
        return {
            "session": None,
            "error": f"会话 {session_id} 不存在",
        }

    # 查询该会话的记忆
    # 假设记忆中有 session_id 字段
    recent_memories = []
    try:
        # 从 memory.jsonl 中查找该 session 的记录
        memory_path = Path(agent.config.memory_path)
        if memory_path.exists():
            lines = memory_path.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines[-10:]):  # 最近 10 条
                try:
                    import json
                    record = json.loads(line)
                    if record.get("session_id") == session_id:
                        recent_memories.append(record)
                except (json.JSONDecodeError, KeyError):
                    continue
    except (OSError, UnicodeDecodeError):
        pass

    # 查询该会话的子代理任务
    subagent_context = []
    try:
        from ..subagent import SubagentRegistry
        registry = SubagentRegistry(agent)
        board = registry.build_board(recent_limit=5)
        for item in board.recent:
            # 假设子代理记录中有 session_id
            if hasattr(item, "metadata") and item.metadata.get("session_id") == session_id:
                subagent_context.append({
                    "id": item.id,
                    "goal": item.goal,
                    "status": item.status,
                })
    except Exception:
        pass

    return {
        "session": session,
        "recent_memories": recent_memories,
        "subagent_context": subagent_context,
    }


def format_resume_context(resume_data: dict) -> str:
    """格式化恢复上下文为文本。

    Args:
        resume_data: resume_session 返回的数据

    Returns:
        格式化的文本
    """
    if resume_data.get("error"):
        return f"恢复失败: {resume_data['error']}"

    session = resume_data.get("session")
    if not session:
        return "会话不存在"

    lines = [
        f"## 会话恢复上下文",
        f"会话 ID: {session.session_id}",
        f"创建时间: {session.created_at}",
        f"最后活跃: {session.updated_at}",
        f"活跃通道: {session.last_active_channel}",
    ]

    memories = resume_data.get("recent_memories", [])
    if memories:
        lines.append(f"\n### 最近记忆 ({len(memories)} 条)")
        for mem in memories[:3]:  # 最多显示 3 条
            lines.append(f"- [{mem.get('role', 'unknown')}] {mem.get('content', '')[:100]}...")

    subagents = resume_data.get("subagent_context", [])
    if subagents:
        lines.append(f"\n### 子代理任务 ({len(subagents)} 条)")
        for sub in subagents[:3]:
            lines.append(f"- {sub['id']}: {sub['goal']} ({sub['status']})")

    if not memories and not subagents:
        lines.append("\n暂无历史记录")

    return "\n".join(lines)


__all__ = [
    "resume_session",
    "format_resume_context",
]
