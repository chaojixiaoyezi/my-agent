

from __future__ import annotations

from collections.abc import Iterable  # noqa: F401  # re-exported for backwards compatibility
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import RawMemoryEvent, utc_now_iso
from ..storage import append_raw_event
from ..tokens import estimate_tokens
from .event_builders import (
    EventIdentity,
    MessageContext,
    ToolCallContext,
    _content_hash,
    _event_id,
    _message_event,
    _normalize_archive_level,
    _normalize_tool_call,
    _preview,
    _tool_event,
)


@dataclass(frozen=True)
class ArchiveRunTurnResult:

    write_paths: tuple[Path, ...]
    event_count: int
    token_estimate: int
    event_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    events: tuple[RawMemoryEvent, ...]

    @property
    def paths(self) -> tuple[Path, ...]:

        return self.write_paths

    def to_dict(self) -> dict[str, Any]:

        return {
            "write_paths": [str(path) for path in self.write_paths],
            "event_count": self.event_count,
            "token_estimate": self.token_estimate,
            "event_ids": list(self.event_ids),
            "content_hashes": list(self.content_hashes),
            "events": [event.to_dict() for event in self.events],
        }

    def __getitem__(self, key: str) -> Any:

        return self.to_dict()[key]


@dataclass(frozen=True)
class TurnData:
    """Bundle of user/assistant content for one turn."""
    user_prompt: str
    response_text: str
    tool_calls: list[dict[str, Any]]


@dataclass(frozen=True)
class RunContext:
    """Shared runtime fields for turn archiving."""
    backend: str
    request_id: str
    run_id: str
    task_id: str
    source: str
    archive_level: int
    created_at: str
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


def _build_run_turn_events(
    session_id: str,
    turn: TurnData,
    ctx: RunContext,
) -> list[RawMemoryEvent]:
    """Convert one run turn into ordered RawMemoryEvent objects without writing them."""
    return [
        *_message_events(session_id, turn, ctx),
        *_tool_events(session_id, turn, ctx),
    ]


def _message_events(session_id: str, turn: TurnData, ctx: RunContext) -> list[RawMemoryEvent]:
    """Build user and assistant message events for one turn."""
    return [
        _message_event(
            EventIdentity(
                sequence=1,
                session_id=session_id,
                request_id=ctx.request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            ),
            MessageContext(
                speaker="user",
                target="assistant",
                action="message",
                backend=ctx.backend,
                source=ctx.source,
                archive_level=ctx.archive_level,
                created_at=ctx.created_at,
                content=turn.user_prompt,
                preview_limits=ctx.preview_limits,
                summary_chars=ctx.summary_chars,
            ),
        ),
        _message_event(
            EventIdentity(
                sequence=2,
                session_id=session_id,
                request_id=ctx.request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            ),
            MessageContext(
                speaker="assistant",
                target="user",
                action="response",
                backend=ctx.backend,
                source=ctx.source,
                archive_level=ctx.archive_level,
                created_at=ctx.created_at,
                content=turn.response_text,
                preview_limits=ctx.preview_limits,
                summary_chars=ctx.summary_chars,
            ),
        ),
    ]


def _tool_events(session_id: str, turn: TurnData, ctx: RunContext) -> list[RawMemoryEvent]:
    """Build tool events for one turn."""
    events: list[RawMemoryEvent] = []
    for index, tool_call in enumerate(turn.tool_calls, start=1):
        if _already_live_archived(tool_call):
            continue
        events.append(
            _tool_event(
                EventIdentity(
                    sequence=index,
                    session_id=session_id,
                    request_id=ctx.request_id,
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                ),
                ToolCallContext(
                    backend=ctx.backend,
                    tool_call=tool_call,
                    source=ctx.source,
                    archive_level=ctx.archive_level,
                    created_at=ctx.created_at,
                    preview_limits=ctx.preview_limits,
                ),
            )
        )

    return events


def _already_live_archived(tool_call: dict[str, Any]) -> bool:
    return bool(tool_call.get("raw_archive_event_id") or tool_call.get("raw_archive_path"))


@dataclass(frozen=True)
class ArchiveTurnContext:
    """Context for archiving a single turn."""
    session_id: str
    user_prompt: str
    response_text: str
    backend: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    archive_level: int = 3
    created_at: str | None = None
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


@dataclass(frozen=True)
class ArchiveRunTurnParams:
    """Parameter bundle for archive_run_turn."""
    root: str | Path
    ctx: ArchiveTurnContext


def archive_run_turn(
    params: ArchiveRunTurnParams,
) -> ArchiveRunTurnResult:
    """Append user, assistant, and optional tool raw archive events for one run turn."""
    ctx = params.ctx
    root = params.root

    normalized_level = _normalize_archive_level(ctx.archive_level)
    timestamp = ctx.created_at or utc_now_iso()
    normalized_tool_calls = [_normalize_tool_call(call) for call in ctx.tool_calls or []]
    events = _build_run_turn_events(
        session_id=str(ctx.session_id),
        turn=TurnData(
            user_prompt=str(ctx.user_prompt),
            response_text=str(ctx.response_text),
            tool_calls=normalized_tool_calls,
        ),
        ctx=RunContext(
            backend=str(ctx.backend),
            request_id=str(ctx.request_id),
            run_id=str(ctx.run_id),
            task_id=str(ctx.task_id),
            source=str(ctx.source),
            archive_level=normalized_level,
            created_at=timestamp,
            preview_limits=ctx.preview_limits,
            summary_chars=ctx.summary_chars,
        ),
    )

    paths = _append_events(root, events)
    token_estimate = _turn_token_estimate(ctx, normalized_tool_calls)

    return ArchiveRunTurnResult(
        write_paths=tuple(paths),
        event_count=len(events),
        token_estimate=token_estimate,
        event_ids=tuple(event.event_id for event in events),
        content_hashes=tuple(event.content_hash for event in events),
        events=tuple(events),
    )


def _append_events(root: str | Path, events: list[RawMemoryEvent]) -> list[Path]:
    paths: list[Path] = []
    for event in events:
        path = append_raw_event(root, event)
        if path not in paths:
            paths.append(path)
    return paths


def _turn_token_estimate(ctx: ArchiveTurnContext, tool_calls: list[dict[str, Any]]) -> int:
    return estimate_tokens(
        {
            "user_prompt": ctx.user_prompt,
            "response_text": ctx.response_text,
            "backend": ctx.backend,
            "tool_calls": tool_calls,
        }
    )
