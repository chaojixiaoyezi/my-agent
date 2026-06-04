

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..models import RawMemoryEvent
from ._event_utils import (
    _canonical_json,
    _content_hash,
    _event_id,
    _first_bool,
    _first_text,
    _normalize_archive_level,
    _normalize_tool_call,
    _preview,
    _stable_display_json,
    _summarize_text,
    _tool_status,
)


@dataclass
class EventIdentity:
    """Shared identity fields for message and tool events."""
    sequence: int
    session_id: str
    request_id: str
    run_id: str
    task_id: str


@dataclass
class MessageContext:
    """Context fields specific to a message event."""
    speaker: str
    target: str
    action: str
    backend: str
    source: str
    archive_level: int
    created_at: str
    content: str
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


@dataclass
class ToolCallContext:
    """Context fields specific to a tool-call event."""
    backend: str
    tool_call: dict[str, Any]
    source: str
    archive_level: int
    created_at: str
    preview_limits: dict[int, int] | None = None


@dataclass(frozen=True)
class _ToolEventFields:
    """Normalized fields shared by tool event id, metadata, and RawMemoryEvent."""
    tool_name: str
    tool_call_id: str
    tool_success: bool | None
    status: str
    error_code: str
    metadata: dict[str, Any]
    metadata_text: str
    content_hash: str


@dataclass(frozen=True)
class _ToolFacts:
    """Bundle for _tool_metadata keyword parameters."""

    tool_name: str
    tool_call_id: str
    tool_success: bool | None
    status: str
    error_code: str
    backend: str
    preview_limits: dict[int, int] | None = None


def _message_event(
    identity: EventIdentity,
    ctx: MessageContext,
) -> RawMemoryEvent:
    content = ctx.content
    content_hash = _content_hash(content)
    event_id = _event_id(
        {
            "kind": "message",
            "sequence": identity.sequence,
            "session_id": identity.session_id,
            "request_id": identity.request_id,
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "speaker": ctx.speaker,
            "target": ctx.target,
            "action": ctx.action,
            "backend": ctx.backend,
            "source": ctx.source,
            "content_hash": content_hash,
        }
    )
    event = RawMemoryEvent(
        event_id=event_id,
        session_id=identity.session_id,
        request_id=identity.request_id,
        run_id=identity.run_id,
        speaker=ctx.speaker,
        target=ctx.target,
        action=ctx.action,
        created_at=ctx.created_at,
        status="ok",
        task_id=identity.task_id,
        content_preview=_preview(content, ctx.archive_level, ctx.preview_limits),
        content_path="",
        content_hash=content_hash,
        visibility="private",
        source=ctx.source,
        archive_level=_normalize_archive_level(ctx.archive_level),
    )
    return _apply_archive_level_to_message_event(
        event,
        content=content,
        summary_chars=ctx.summary_chars,
        preview_limits=ctx.preview_limits,
    )


def _tool_event(
    identity: EventIdentity,
    ctx: ToolCallContext,
) -> RawMemoryEvent:
    tool_call = ctx.tool_call
    fields = _tool_event_fields(tool_call, backend=ctx.backend, preview_limits=ctx.preview_limits)
    event = _tool_raw_event(identity, ctx, fields)
    return _apply_archive_level_to_tool_event(
        event,
        tool_call=tool_call,
        metadata=fields.metadata,
        preview_limits=ctx.preview_limits,
    )


def _tool_raw_event(
    identity: EventIdentity,
    ctx: ToolCallContext,
    fields: _ToolEventFields,
) -> RawMemoryEvent:
    event_id = _event_id(
        {
            "kind": "tool",
            "sequence": identity.sequence,
            "session_id": identity.session_id,
            "request_id": identity.request_id,
            "run_id": identity.run_id,
            "task_id": identity.task_id,
            "tool_name": fields.tool_name,
            "tool_call_id": fields.tool_call_id,
            "backend": ctx.backend,
            "source": ctx.source,
            "content_hash": fields.content_hash,
        }
    )
    event = RawMemoryEvent(
        event_id=event_id,
        session_id=identity.session_id,
        request_id=identity.request_id,
        run_id=identity.run_id,
        speaker="tool",
        target="assistant",
        action="tool_call",
        created_at=ctx.created_at,
        status=fields.status,
        error_code=fields.error_code,
        is_dispatch=bool(ctx.tool_call.get("is_dispatch", False)),
        task_id=identity.task_id,
        tool_name=fields.tool_name,
        tool_call_id=fields.tool_call_id,
        tool_success=fields.tool_success,
        content_preview=_preview(fields.metadata_text, ctx.archive_level, ctx.preview_limits),
        content_path=str(fields.metadata.get("output_path") or fields.metadata.get("content_path") or ""),
        content_hash=fields.content_hash,
        visibility="private",
        source=ctx.source,
        archive_level=_normalize_archive_level(ctx.archive_level),
    )
    return event


def _tool_event_fields(
    tool_call: dict[str, Any],
    *,
    backend: str,
    preview_limits: dict[int, int] | None = None,
) -> _ToolEventFields:
    """Normalize tool-call facts before creating the archive event."""
    tool_name = _first_text(tool_call, "tool_name", "tool", "name") or "unknown"
    tool_call_id = _first_text(tool_call, "tool_call_id", "call_id", "id")
    tool_success = _first_bool(tool_call, "tool_success", "success", "ok")
    status = _tool_status(tool_call, tool_success)
    error_code = _first_text(tool_call, "error_code", "code")
    metadata = _tool_metadata(
        tool_call,
        facts=_ToolFacts(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_success=tool_success,
            status=status,
            error_code=error_code,
            backend=backend,
            preview_limits=preview_limits,
        ),
    )
    return _ToolEventFields(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_success=tool_success,
        status=status,
        error_code=error_code,
        metadata=metadata,
        metadata_text=_stable_display_json(metadata),
        content_hash=_content_hash(_canonical_json(tool_call)),
    )


def _tool_metadata(
    tool_call: dict[str, Any],
    *,
    facts: _ToolFacts,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": facts.tool_name,
        "tool_call_id": facts.tool_call_id,
        "success": facts.tool_success,
        "status": facts.status,
        "error_code": facts.error_code,
        "backend": facts.backend,
    }
    for key in ("output", "result", "response", "content"):
        if key in tool_call:
            text = str(tool_call[key])
            metadata[f"{key}_hash"] = _content_hash(text)
            metadata[f"{key}_preview"] = _preview(text, 2, facts.preview_limits)
            break
    for key in ("output_hash", "output_preview", "output_path", "output_externalized", "output_size_bytes"):
        if key in tool_call:
            metadata[key] = tool_call[key]
    for key in ("parameters", "params", "arguments", "args"):
        if key in tool_call:
            metadata[key] = tool_call[key]
            break
    return metadata


def _apply_archive_level_to_message_event(
    event: RawMemoryEvent,
    *,
    content: str,
    summary_chars: int = 96,
    preview_limits: dict[int, int] | None = None,
) -> RawMemoryEvent:

    if event.archive_level == 0:
        return event
    if event.archive_level == 1:
        if event.speaker == "assistant":
            event.content_hash = ""
        return event
    if event.archive_level == 2:
        event.content_preview = _summarize_text(content, default=event.action, limit=summary_chars)
        event.content_hash = ""
        return event
    event.content_preview = _preview(content, event.archive_level, preview_limits)
    return event


def _apply_archive_level_to_tool_event(
    event: RawMemoryEvent,
    *,
    tool_call: dict[str, Any],
    metadata: dict[str, Any],
    preview_limits: dict[int, int] | None = None,
) -> RawMemoryEvent:

    if event.archive_level == 0:
        return event
    if event.archive_level == 1:
        event.content_preview = _stable_display_json(
            {
                "tool_name": metadata.get("tool_name", ""),
                "tool_call_id": metadata.get("tool_call_id", ""),
                "success": metadata.get("success"),
                "status": metadata.get("status", ""),
                "error_code": metadata.get("error_code", ""),
            }
        )
        return event
    if event.archive_level == 2:
        event.content_preview = f"{event.tool_name}:{event.status or 'unknown'}"
        event.content_hash = ""
        return event
    event.content_preview = _preview(_stable_display_json(metadata), event.archive_level, preview_limits)
    return event
