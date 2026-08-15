
"""CompressionSnapshot object builders for recovery and compression snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import CompressionSnapshot
from ._helpers import _content_hash, _participants, _preview, _snapshot_id


@dataclass(frozen=True)
class MakeRecoverySnapshotIdParams:
    """Params bundle for _make_recovery_snapshot_id."""
    timestamp: str
    session_id: str
    request_id: str
    run_id: str
    task_id: str
    source: str
    status: str
    user_prompt: str
    response_text: str


@dataclass(frozen=True)
class RecoverySnapshotBuildParams:
    """Internal params bundle for _build_recovery_snapshot."""
    snapshot_id: str
    session_id: str
    request_id: str
    run_id: str
    task_id: str
    source: str
    status: str
    error_code: str
    backend: str
    normalized_tools: list[dict[str, Any]]
    user_prompt: str
    response_text: str
    level: int
    token_estimate: int
    clean_task_refs: list[str]
    clean_next_actions: list[str]
    clean_content_paths: list[str]
    timestamp: str


@dataclass(frozen=True)
class CompressionSnapshotBuildParams:
    """Internal params bundle for _build_compression_snapshot."""
    snapshot_id: str
    session_id: str
    turn_id: str
    source: str
    request_id: str
    run_id: str
    task_id: str
    backend: str
    role: str
    normalized_tools: list[dict[str, Any]]
    token_estimate: int
    level: int
    clean_task_refs: list[str]
    clean_next_actions: list[str]
    clean_content_paths: list[str]
    content: str
    timestamp: str


def _make_recovery_snapshot_id(*, params: MakeRecoverySnapshotIdParams) -> str:
    """Build snapshot ID for recovery snapshots."""
    return _snapshot_id(
        {
            "created_at": params.timestamp,
            "session_id": params.session_id,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
            "source": params.source,
            "status": params.status,
            "user_hash": _content_hash(params.user_prompt),
            "response_hash": _content_hash(params.response_text),
        }
    )


def _build_recovery_snapshot(*, params: RecoverySnapshotBuildParams) -> CompressionSnapshot:
    """Build a CompressionSnapshot for recovery snapshot."""
    return CompressionSnapshot(
        snapshot_id=params.snapshot_id,
        session_id=str(params.session_id),
        compression_id=f"recovery:{params.source}:{params.request_id or params.run_id or params.task_id or params.snapshot_id[-8:]}",
        turn_range={
            "kind": "recovery_snapshot",
            "source": params.source,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
        },
        participants=_participants(params.normalized_tools),
        user_intents=[_preview(params.user_prompt, params.level)] if params.user_prompt else [],
        assistant_actions=[_preview(params.response_text, params.level)] if params.response_text else [],
        tool_calls=params.normalized_tools,
        dispatch_events=[
            {
                "source": params.source,
                "request_id": params.request_id,
                "run_id": params.run_id,
                "task_id": params.task_id,
                "status": params.status,
                "error_code": params.error_code,
                "backend": params.backend,
            }
        ],
        task_refs=params.clean_task_refs,
        decisions=[],
        open_questions=[],
        next_actions=params.clean_next_actions,
        token_usage={"estimate": params.token_estimate, "archive_level": params.level, "backend": params.backend},
        archive_level=params.level,
        content_paths=params.clean_content_paths,
        created_at=params.timestamp,
    )


def _build_compression_snapshot(*, params: CompressionSnapshotBuildParams) -> CompressionSnapshot:
    """Build a CompressionSnapshot for compression snapshot."""
    return CompressionSnapshot(
        snapshot_id=params.snapshot_id,
        session_id=str(params.session_id),
        compression_id=f"compression:{params.request_id or params.run_id or params.task_id or params.turn_id or params.snapshot_id[-8:]}",
        turn_range={
            "kind": "compression_snapshot",
            "turn_id": params.turn_id,
            "source": params.source,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
        },
        participants=[params.role] if params.role else ["system"],
        user_intents=[_preview(params.content, params.level)] if params.role == "user" and params.content else [],
        assistant_actions=[_preview(params.content, params.level)] if params.role == "assistant" and params.content else [],
        tool_calls=params.normalized_tools,
        dispatch_events=[
            {
                "source": params.source,
                "request_id": params.request_id,
                "run_id": params.run_id,
                "task_id": params.task_id,
                "backend": params.backend,
                "status": "snapshot_written",
            }
        ],
        task_refs=params.clean_task_refs,
        next_actions=params.clean_next_actions,
        token_usage={"estimate": params.token_estimate, "archive_level": params.level, "backend": params.backend},
        archive_level=params.level,
        content_paths=params.clean_content_paths,
        turn_id=str(params.turn_id),
        role=str(params.role or "system"),
        content=_preview(params.content, params.level),
        token_estimate=params.token_estimate,
        timestamp=params.timestamp,
        created_at=params.timestamp,
    )
