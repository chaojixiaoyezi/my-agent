# LLM: Live raw archiver writes incremental run facts into the existing raw archive, not a second ledger.
# 模块用途: 让运行中工具轮和工具结果边发生边写入 raw archive，避免等最终收尾才有黑匣子记录。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import RawMemoryEvent, utc_now_iso
from ..storage import append_raw_event
from ..tokens import estimate_tokens
from .event_builders import (
    EventIdentity,
    MessageContext,
    ToolCallContext,
    _message_event,
    _tool_event,
)
from .turn_archiver import ArchiveRunTurnResult


# LLM: ArchiveAssistantToolRoundParams is the public bundle for one assistant tool-call round.
# 类用途: 保存一轮模型工具调用前的可见文字、调用列表和运行身份，供 live raw archive 写入。
@dataclass(frozen=True)
class ArchiveAssistantToolRoundParams:
    root: str | Path
    session_id: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    tool_round: int = 0
    response_text: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    backend: str = "tool_loop"
    source: str = "live_tool_loop"
    archive_level: int = 3
    created_at: str = ""
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


# LLM: ArchiveLiveToolCallParams is the public bundle for one completed tool result.
# 类用途: 保存工具结果归档记录和运行身份，供 live raw archive 在工具返回后立即写入。
@dataclass(frozen=True)
class ArchiveLiveToolCallParams:
    root: str | Path
    session_id: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    tool_round: int = 0
    tool_index: int = 0
    tool_record: dict[str, Any] | None = None
    backend: str = "tool_loop"
    source: str = "live_tool_loop"
    archive_level: int = 3
    created_at: str = ""
    preview_limits: dict[int, int] | None = None


# LLM: archive_assistant_tool_round appends one visible assistant tool-round note to raw archive.
# 函数用途: 记录模型在调用工具前说了什么、准备调用哪些工具；不记录隐藏思考链。
def archive_assistant_tool_round(params: ArchiveAssistantToolRoundParams) -> ArchiveRunTurnResult:
    timestamp = params.created_at or utc_now_iso()
    tool_names = [
        str(item.get("tool") or item.get("name") or "unknown")
        for item in params.tool_calls or []
        if isinstance(item, dict)
    ]
    content = _assistant_round_content(params.response_text, tool_names)
    event = _message_event(
        EventIdentity(
            sequence=max(0, int(params.tool_round)),
            session_id=str(params.session_id),
            request_id=str(params.request_id),
            run_id=str(params.run_id),
            task_id=str(params.task_id),
        ),
        MessageContext(
            speaker="assistant",
            target="tool_loop",
            action="assistant_tool_round",
            backend=str(params.backend),
            source=str(params.source),
            archive_level=int(params.archive_level),
            created_at=timestamp,
            content=content,
            preview_limits=params.preview_limits,
            summary_chars=int(params.summary_chars),
        ),
    )
    return _append_live_events(params.root, [event], token_payload={"content": content, "tool_names": tool_names})


# LLM: archive_live_tool_call appends one completed tool result to raw archive.
# 函数用途: 复用现有 tool raw event 结构，工具完成即写，避免中途崩溃时只剩内存记录。
def archive_live_tool_call(params: ArchiveLiveToolCallParams) -> ArchiveRunTurnResult:
    timestamp = params.created_at or utc_now_iso()
    record = dict(params.tool_record or {})
    call_id = str(record.get("id") or record.get("call_id") or record.get("tool_call_id") or "")
    if not call_id:
        call_id = f"{int(params.tool_round)}-{int(params.tool_index)}"
        record.setdefault("id", call_id)
    event = _tool_event(
        EventIdentity(
            sequence=max(0, int(params.tool_round)) * 1000 + max(0, int(params.tool_index)),
            session_id=str(params.session_id),
            request_id=str(params.request_id),
            run_id=str(params.run_id),
            task_id=str(params.task_id),
        ),
        ToolCallContext(
            backend=str(params.backend),
            tool_call=record,
            source=str(params.source),
            archive_level=int(params.archive_level),
            created_at=timestamp,
            preview_limits=params.preview_limits,
        ),
    )
    return _append_live_events(params.root, [event], token_payload=record)


# LLM: _assistant_round_content keeps live assistant tool-round records compact and readable.
# 函数用途: 合并模型可见文字和即将调用的工具名，写入 raw archive 的 content_preview。
def _assistant_round_content(response_text: str, tool_names: list[str]) -> str:
    lines = [str(response_text or "").strip()]
    if tool_names:
        lines.append("tools: " + ", ".join(tool_names))
    return "\n".join(line for line in lines if line)


# LLM: _append_live_events appends already-built raw events and returns the same result shape as turn archiving.
# 函数用途: 写入 live raw archive 事件并返回路径、事件 id、hash 和 token 估算。
def _append_live_events(root: str | Path, events: list[RawMemoryEvent], *, token_payload: object) -> ArchiveRunTurnResult:
    paths: list[Path] = []
    for event in events:
        path = append_raw_event(root, event)
        if path not in paths:
            paths.append(path)
    return ArchiveRunTurnResult(
        write_paths=tuple(paths),
        event_count=len(events),
        token_estimate=estimate_tokens(token_payload),
        event_ids=tuple(event.event_id for event in events),
        content_hashes=tuple(event.content_hash for event in events),
        events=tuple(events),
    )


__all__ = [
    "ArchiveAssistantToolRoundParams",
    "ArchiveLiveToolCallParams",
    "archive_assistant_tool_round",
    "archive_live_tool_call",
]
