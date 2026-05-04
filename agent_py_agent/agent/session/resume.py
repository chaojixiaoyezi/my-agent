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
    """恢复会话上下文."""
    manager = SessionManager(agent.config)
    session = manager.load_session(session_id)
    if session is None:
        return {"session": None, "error": f"会话 {session_id} 不存在"}
    recent_memories = _load_recent_memories(agent.config, session_id)
    subagent_context = _load_subagent_context(agent, session_id)
    return {"session": session, "recent_memories": recent_memories, "subagent_context": subagent_context}


def _load_recent_memories(config, session_id: str) -> list[dict]:
    """Load recent memories for a session from memory.jsonl."""
    recent_memories: list[dict] = []
    try:
        memory_path = Path(config.memory_path)
        if memory_path.exists():
            lines = memory_path.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines[-10:]):
                record = _parse_memory_line(line, session_id)
                if record:
                    recent_memories.append(record)
    except (OSError, UnicodeDecodeError):
        pass
    return recent_memories


def _parse_memory_line(line: str, session_id: str) -> dict | None:
    """Parse one memory line and return it if session_id matches."""
    if not line:
        return None
    try:
        record = json.loads(line)
        if record.get("session_id") == session_id:
            return record
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _load_subagent_context(agent: SimpleAgent, session_id: str) -> list[dict]:
    """Load subagent context for a session."""
    subagent_context: list[dict] = []
    try:
        from ..subagent import SubagentRegistry

        registry = SubagentRegistry(agent)
        board = registry.build_board(recent_limit=5)
        for item in board.recent:
            if hasattr(item, "metadata") and item.metadata.get("session_id") == session_id:
                subagent_context.append({
                    "id": item.id,
                    "goal": item.goal,
                    "status": item.status,
                })
    except Exception:
        pass
    return subagent_context


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
        "## 会话恢复上下文",
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
