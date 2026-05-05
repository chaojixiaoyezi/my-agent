"""LLM: low-level event builders, normalizers, and hashing helpers for run-turn archiving.

给人看的解释：
这个文件放所有"构造单条归档事件"和"辅助工具函数"。
包括用户/助手消息事件、工具调用事件、工具元数据构建，
以及归一化、预览裁剪、hash、稳定 JSON 序列化等纯函数。
主入口 turn_archiver.archive_run_turn 只调用 _build_run_turn_events，不直接碰磁盘。
"""

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


@dataclass
class ToolCallContext:
    """Context fields specific to a tool-call event."""
    backend: str
    tool_call: dict[str, Any]
    source: str
    archive_level: int
    created_at: str


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


def _message_event(
    identity: EventIdentity,
    ctx: MessageContext,
) -> RawMemoryEvent:
    """LLM: create one user or assistant message archive event.

    新手说明:
    用户消息和助手回复字段形状基本一样，只是 speaker、target 和 action 不同。
    这里统一生成 event_id、短预览和内容 hash，避免两边格式漂移。
    """
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
        content_preview=_preview(content, ctx.archive_level),
        content_path="",
        content_hash=content_hash,
        visibility="private",
        source=ctx.source,
        archive_level=_normalize_archive_level(ctx.archive_level),
    )
    return _apply_archive_level_to_message_event(event, content=content)


def _tool_event(
    identity: EventIdentity,
    ctx: ToolCallContext,
) -> RawMemoryEvent:
    """LLM: create one tool-call archive event from normalized tool metadata.

    新手说明:
    工具调用可能来自不同后端，字段名不完全一样。这个函数先提取常见字段，
    再把输出正文变成 hash 和短预览，避免 raw archive 暴涨。
    """
    tool_call = ctx.tool_call
    fields = _tool_event_fields(tool_call, backend=ctx.backend)
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
        is_dispatch=bool(tool_call.get("is_dispatch", False)),
        task_id=identity.task_id,
        tool_name=fields.tool_name,
        tool_call_id=fields.tool_call_id,
        tool_success=fields.tool_success,
        content_preview=_preview(fields.metadata_text, ctx.archive_level),
        content_path="",
        content_hash=fields.content_hash,
        visibility="private",
        source=ctx.source,
        archive_level=_normalize_archive_level(ctx.archive_level),
    )
    return _apply_archive_level_to_tool_event(event, tool_call=tool_call, metadata=fields.metadata)


def _tool_event_fields(tool_call: dict[str, Any], *, backend: str) -> _ToolEventFields:
    """Normalize tool-call facts before creating the archive event."""
    tool_name = _first_text(tool_call, "tool_name", "tool", "name") or "unknown"
    tool_call_id = _first_text(tool_call, "tool_call_id", "call_id", "id")
    tool_success = _first_bool(tool_call, "tool_success", "success", "ok")
    status = _tool_status(tool_call, tool_success)
    error_code = _first_text(tool_call, "error_code", "code")
    metadata = _tool_metadata(
        tool_call,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_success=tool_success,
        status=status,
        error_code=error_code,
        backend=backend,
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
    tool_name: str,
    tool_call_id: str,
    tool_success: bool | None,
    status: str,
    error_code: str,
    backend: str,
) -> dict[str, Any]:
    """LLM: build the bounded metadata preview stored for a tool event.

    新手说明:
    工具结果可能很长，甚至包含敏感内容。这里只保留状态、参数和输出摘要；
    完整输出以后应走 content_path 或 evidence 文件，而不是塞进 raw event。
    """
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "success": tool_success,
        "status": status,
        "error_code": error_code,
        "backend": backend,
    }
    for key in ("output", "result", "response", "content"):
        if key in tool_call:
            text = str(tool_call[key])
            metadata[f"{key}_hash"] = _content_hash(text)
            metadata[f"{key}_preview"] = _preview(text, 2)
            break
    for key in ("parameters", "params", "arguments", "args"):
        if key in tool_call:
            metadata[key] = tool_call[key]
            break
    return metadata


def _apply_archive_level_to_message_event(event: RawMemoryEvent, *, content: str) -> RawMemoryEvent:
    """LLM: shape message archive granularity according to the configured archive level.

    新手说明:
    0 最完整，3 最精简。这里不改 JSONL 格式，只改字段保留多少细节。
    """

    if event.archive_level == 0:
        return event
    if event.archive_level == 1:
        if event.speaker == "assistant":
            event.content_hash = ""
        return event
    if event.archive_level == 2:
        event.content_preview = _summarize_text(content, fallback=event.action)
        event.content_hash = ""
        return event
    event.content_preview = _preview(content, event.archive_level)
    return event


def _apply_archive_level_to_tool_event(
    event: RawMemoryEvent,
    *,
    tool_call: dict[str, Any],
    metadata: dict[str, Any],
) -> RawMemoryEvent:
    """LLM: downsample tool archive detail according to archive level.

    新手说明:
    级别越高，越只保留恢复最小集，避免把大段工具输出预览反复写进 raw archive。
    """

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
    event.content_preview = _preview(_stable_display_json(metadata), event.archive_level)
    return event
