
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime_errors import DataCorruptionError, runtime_error_report

if TYPE_CHECKING:
    from ..core import SimpleAgent

from .manager import SessionManager


def resume_session(agent: SimpleAgent, session_id: str) -> dict:
    """恢复会话上下文."""
    manager = SessionManager(agent.config)
    session = manager.load_session(session_id)
    if session is None:
        return {"session": None, "error": f"会话 {session_id} 不存在"}
    recent_memories, recent_memory_load_errors = _load_recent_memories_report(agent.config, session_id)
    subagent_context = _load_subagent_context(agent, session_id, metadata=session.metadata)
    return {
        "session": session,
        "recent_memories": recent_memories,
        "recent_memory_load_errors": recent_memory_load_errors,
        "subagent_context": subagent_context,
    }


def _load_recent_memories(config, session_id: str) -> list[dict]:
    """Load recent memories for a session from memory.jsonl."""
    return _load_recent_memories_report(config, session_id)[0]


def _load_recent_memories_report(config, session_id: str) -> tuple[list[dict], list[dict]]:
    try:
        memory_path = Path(config.memory_path)
    except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        return [], [_memory_load_error(exc, context="session.resume.memory_path")]
    if not memory_path.exists():
        return [], []
    return _recent_memory_records_report(memory_path, session_id)


def _recent_memory_records(memory_path: Path, session_id: str) -> list[dict]:
    return _recent_memory_records_report(memory_path, session_id)[0]


def _recent_memory_records_report(memory_path: Path, session_id: str) -> tuple[list[dict], list[dict]]:
    try:
        lines = memory_path.read_text(encoding="utf-8").strip().split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [_memory_load_error(exc, context="session.resume.memory_file", path=memory_path)]
    records: list[dict] = []
    load_errors: list[dict] = []
    for offset, line in enumerate(reversed(lines[-10:]), start=1):
        record, load_error = _parse_memory_line_report(line, session_id, memory_path=memory_path, offset=offset)
        if record:
            records.append(record)
        if load_error:
            load_errors.append(load_error)
    return records, load_errors


def _parse_memory_line(line: str, session_id: str) -> dict | None:
    """Parse one memory line and return it if session_id matches."""
    return _parse_memory_line_report(line, session_id, memory_path=None, offset=0)[0]


def _parse_memory_line_report(
    line: str,
    session_id: str,
    *,
    memory_path: Path | None,
    offset: int,
) -> tuple[dict | None, dict | None]:
    if not line:
        return None, None
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _memory_load_error(exc, context="session.resume.memory_line", path=memory_path, offset=offset)
    if not isinstance(record, dict):
        exc = DataCorruptionError("memory JSONL record must be an object")
        return None, _memory_load_error(exc, context="session.resume.memory_line", path=memory_path, offset=offset)
    if record.get("session_id") == session_id:
        return record, None
    return None, None


def _memory_load_error(
    exc: BaseException,
    *,
    context: str,
    path: Path | None = None,
    offset: int = 0,
) -> dict:
    report = runtime_error_report(exc, context=context)
    if path is not None:
        report["path"] = str(path)
    if offset:
        report["recent_line_offset"] = offset
    return report


def _load_subagent_context(agent: SimpleAgent, session_id: str, *, metadata: dict | None = None) -> list[dict]:
    """Load subagent context for a session."""
    subagent_context: list[dict] = []
    manager = getattr(agent, "subagents", None)
    list_runs = getattr(manager, "list_runs", None)
    if not callable(list_runs):
        return subagent_context
    try:
        from ..subagents.manager import SubAgentManager

        tasks = (_recent_session_runs(manager, session_id, metadata or {})
                 if isinstance(manager, SubAgentManager) else list_runs()[-5:])
    except Exception as exc:
        subagent_context.append({
            "type": "subagent_context_load_error",
            **runtime_error_report(exc, context="session.resume.subagent_context"),
        })
        return subagent_context
    if not isinstance(tasks, list | tuple):
        return subagent_context
    for item in tasks:
        _append_subagent_context_item(subagent_context, item, session_id, metadata=metadata)
    return subagent_context


def _recent_session_runs(manager, session_id, metadata):
    from ..subagents.manager import creation_run_index
    from ..subagents.services.persistence.run_index import SubagentRunIndexUnavailable

    identity = manager.persistence._run_index_identity()
    index = creation_run_index(manager)
    refs = _session_task_refs(metadata)
    threads = {session_id, str(metadata.get("conversation_thread_id") or "").strip()} - {""}
    selectors = [(value, kind) for value in refs for kind in ("lineage", "request")]
    selectors.extend((thread, "thread") for thread in threads)
    with index.connect() as db:
        db.execute("BEGIN")
        index._require_complete(db)
        sql = " UNION ".join("SELECT run_id FROM membership WHERE scope_id=? AND kind=?" for _ in selectors)
        ids = [row[0] for row in db.execute("SELECT r.run_id FROM runs r JOIN (" + sql +
               ") m ON m.run_id=r.run_id ORDER BY r.updated DESC,r.run_id LIMIT 5",
               tuple(value for pair in selectors for value in pair))]
    manager.persistence._require_run_index_identity(identity)
    report = manager.list_runs_by_ids_report(ids)
    if report.load_errors or any(not _subagent_belongs_to_session(task, session_id, metadata=metadata) for task in report.runs):
        error = SubagentRunIndexUnavailable("session subagent canonical/index unavailable")
        error.load_errors = report.load_errors
        raise error
    return report.runs


def _session_task_refs(metadata):
    return {str(metadata.get(key) or "").strip() for key in ("task_id", "root_task_id", "conversation_task_id", "run_id", "request_id")} - {""}


def _append_subagent_context_item(subagent_context: list[dict], item, session_id: str, *, metadata=None) -> None:
    if not _subagent_belongs_to_session(item, session_id, metadata=metadata):
        return
    subagent_context.append({
        "id": item.id,
        "goal": item.goal,
        "status": item.status,
    })


def _subagent_belongs_to_session(item, session_id: str, *, metadata=None) -> bool:
    attrs = getattr(item, "attributes", None) or {}
    refs = _session_task_refs(metadata or {})
    lineage = {str(value or "") for value in (getattr(item, "id", ""), getattr(item, "root_id", ""), getattr(item, "parent_id", ""),
                                             attrs.get("conversation_task_id"), attrs.get("conversation_request_id"))}
    thread_id = str(getattr(item, "conversation_thread_id", "") or getattr(item, "thread_id", "") or attrs.get("conversation_thread_id") or "")
    if refs & lineage or thread_id and thread_id in {session_id, (metadata or {}).get("conversation_thread_id")}:
        return True
    metadata = getattr(item, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("session_id") == session_id:
        return True
    attributes = getattr(item, "attributes", None)
    if isinstance(attributes, dict) and attributes.get("session_id") == session_id:
        return True
    return session_id in {
        str(getattr(item, "subagent_session_id", "") or ""),
        str(getattr(item, "agent_thread_id", "") or ""),
        str(getattr(item, "root_subagent_session_id", "") or ""),
    }


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

    memory_load_errors = resume_data.get("recent_memory_load_errors", [])
    if memory_load_errors:
        lines.append(f"\n### 最近记忆读取警告 ({len(memory_load_errors)} 条)")
        for error in memory_load_errors[:3]:
            lines.append(f"- {error.get('model_message', error.get('message', 'unknown'))}")

    subagents = resume_data.get("subagent_context", [])
    if subagents:
        lines.append(f"\n### 子代理任务 ({len(subagents)} 条)")
        for sub in subagents[:3]:
            lines.append(_format_subagent_resume_line(sub))

    if not memories and not memory_load_errors and not subagents:
        lines.append("\n暂无历史记录")

    return "\n".join(lines)


def _format_subagent_resume_line(sub: dict) -> str:
    if sub.get("type") == "subagent_context_load_error":
        return f"- 子代理上下文读取失败: {sub.get('model_message', sub.get('message', 'unknown'))}"
    return f"- {sub['id']}: {sub['goal']} ({sub['status']})"


__all__ = [
    "resume_session",
    "format_resume_context",
]
