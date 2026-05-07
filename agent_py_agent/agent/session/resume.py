# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent

from .manager import SessionManager


# LLM: resume_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理恢复会话相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
def resume_session(agent: SimpleAgent, session_id: str) -> dict:
    """恢复会话上下文."""
    manager = SessionManager(agent.config)
    session = manager.load_session(session_id)
    if session is None:
        return {"session": None, "error": f"会话 {session_id} 不存在"}
    recent_memories = _load_recent_memories(agent.config, session_id)
    subagent_context = _load_subagent_context(agent, session_id)
    return {"session": session, "recent_memories": recent_memories, "subagent_context": subagent_context}


# LLM: _load_recent_memories 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 读取或查询recentmemories需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_recent_memories(config, session_id: str) -> list[dict]:
    """Load recent memories for a session from memory.jsonl."""
    try:
        memory_path = Path(config.memory_path)
    except (OSError, UnicodeDecodeError):
        return []
    if not memory_path.exists():
        return []
    return _recent_memory_records(memory_path, session_id)


# LLM: _recent_memory_records 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理recent记忆记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
def _recent_memory_records(memory_path: Path, session_id: str) -> list[dict]:
    try:
        lines = memory_path.read_text(encoding="utf-8").strip().split("\n")
    except (OSError, UnicodeDecodeError):
        return []
    return [
        record
        for line in reversed(lines[-10:])
        if (record := _parse_memory_line(line, session_id))
    ]


# LLM: _parse_memory_line 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 解析并归一化记忆line的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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


# LLM: _load_subagent_context 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 读取或查询子代理上下文需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_subagent_context(agent: SimpleAgent, session_id: str) -> list[dict]:
    """Load subagent context for a session."""
    subagent_context: list[dict] = []
    try:
        from ..subagent import SubagentRegistry

        registry = SubagentRegistry(agent)
        board = registry.build_board(recent_limit=5)
        for item in board.recent:
            _append_subagent_context_item(subagent_context, item, session_id)
    except Exception:
        pass
    return subagent_context


# LLM: _append_subagent_context_item 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 写入子代理上下文条目的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
def _append_subagent_context_item(subagent_context: list[dict], item, session_id: str) -> None:
    if not (hasattr(item, "metadata") and item.metadata.get("session_id") == session_id):
        return
    # LLM: resume context stores only compact subagent facts.
    subagent_context.append({
        "id": item.id,
        "goal": item.goal,
        "status": item.status,
    })


# LLM: format_resume_context 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 渲染或汇总恢复上下文的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def format_resume_context(resume_data: dict) -> str:
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
