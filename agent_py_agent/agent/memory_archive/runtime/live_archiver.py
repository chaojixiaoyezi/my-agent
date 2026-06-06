
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


def archive_assistant_tool_round(params: ArchiveAssistantToolRoundParams) -> ArchiveRunTurnResult:
    timestamp = params.created_at or utc_now_iso()
    tool_names = [
        str(item.get("tool") or "unknown")
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


def _assistant_round_content(response_text: str, tool_names: list[str]) -> str:
    lines = [str(response_text or "").strip()]
    if tool_names:
        lines.append("tools: " + ", ".join(tool_names))
    return "\n".join(line for line in lines if line)


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
