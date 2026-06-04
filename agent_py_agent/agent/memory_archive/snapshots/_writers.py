
"""Snapshot write implementations used by the public snapshots API."""

from __future__ import annotations

from pathlib import Path

from ..models import utc_now_iso
from ..storage import append_snapshot, write_compression_snapshot_file
from ..tokens import estimate_tokens
from ._builders import (
    CompressionSnapshotBuildParams,
    MakeRecoverySnapshotIdParams,
    RecoverySnapshotBuildParams,
    _build_compression_snapshot,
    _build_recovery_snapshot,
    _make_recovery_snapshot_id,
)
from ._helpers import (
    _content_hash,
    _dedupe_texts,
    _normalize_archive_level,
    _snapshot_id,
    _tool_snapshot,
)
from ._types import (
    CompressionHookResult,
    CompressionSnapshotInput,
    RecoverySnapshotInput,
    RecoverySnapshotResult,
)


def _write_recovery_snapshot_impl(root: str | Path, params: RecoverySnapshotInput) -> RecoverySnapshotResult:
    """Write a best-effort recovery snapshot and report any storage failure."""
    timestamp = params.created_at or utc_now_iso()
    level = _normalize_archive_level(params.archive_level)
    normalized_tools = [_tool_snapshot(item, level) for item in params.tool_calls or []]
    token_estimate = _estimate_recovery_tokens(params, normalized_tools)
    snapshot_id = _recovery_snapshot_id(params, timestamp)
    snapshot = _build_recovery_snapshot(
        params=RecoverySnapshotBuildParams(
            snapshot_id=snapshot_id,
            session_id=params.session_id,
            request_id=params.request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            source=params.source,
            status=params.status,
            error_code=params.error_code,
            backend=params.backend,
            normalized_tools=normalized_tools,
            user_prompt=params.user_prompt,
            response_text=params.response_text,
            level=level,
            token_estimate=token_estimate,
            clean_task_refs=_dedupe_texts([params.run_id, params.task_id, *(params.task_refs or [])]),
            clean_next_actions=_dedupe_texts(params.next_actions or []),
            clean_content_paths=_dedupe_texts(params.content_paths or []),
            timestamp=timestamp,
        )
    )
    try:
        path = append_snapshot(root, snapshot)
    except Exception as exc:
        return RecoverySnapshotResult(
            ok=False,
            snapshot_id=snapshot_id,
            token_estimate=token_estimate,
            error=f"{type(exc).__name__}: {exc}",
        )
    return RecoverySnapshotResult(ok=True, snapshot_id=snapshot_id, path=str(path), token_estimate=token_estimate)


def _write_compression_snapshot_impl(root: str | Path, params: CompressionSnapshotInput) -> CompressionHookResult:
    """Write the authoritative compression snapshot and searchable hook entry."""
    timestamp = params.created_at or utc_now_iso()
    level = _normalize_archive_level(params.archive_level)
    normalized_tools = [_tool_snapshot(item, level) for item in params.tool_calls or []]
    token_estimate = _estimate_compression_tokens(params, normalized_tools)
    snapshot_id = _compression_snapshot_id(params, timestamp)
    snapshot = _build_compression_snapshot(
        params=CompressionSnapshotBuildParams(
            snapshot_id=snapshot_id,
            session_id=params.session_id,
            turn_id=params.turn_id,
            source=params.source,
            request_id=params.request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            backend=params.backend,
            role=params.role,
            normalized_tools=normalized_tools,
            token_estimate=token_estimate,
            level=level,
            clean_task_refs=_dedupe_texts([params.run_id, params.task_id, *(params.task_refs or [])]),
            clean_next_actions=_dedupe_texts(params.next_actions or []),
            clean_content_paths=_dedupe_texts(params.content_paths or []),
            content=params.content,
            timestamp=timestamp,
        )
    )
    snapshot_file = write_compression_snapshot_file(root, snapshot)
    hook_path = append_snapshot(root, snapshot)
    return CompressionHookResult(
        snapshot_id=snapshot_id,
        hook_path=str(hook_path),
        snapshot_file_path=str(snapshot_file),
        token_estimate=token_estimate,
        archive_level=level,
    )


def _estimate_recovery_tokens(params: RecoverySnapshotInput, normalized_tools: list[dict[str, object]]) -> int:
    return estimate_tokens(
        {
            "user_prompt": params.user_prompt,
            "response_text": params.response_text,
            "tool_calls": normalized_tools,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
        }
    )


def _estimate_compression_tokens(params: CompressionSnapshotInput, normalized_tools: list[dict[str, object]]) -> int:
    return estimate_tokens(
        {
            "turn_id": params.turn_id,
            "role": params.role,
            "content": params.content,
            "tool_calls": normalized_tools,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
        }
    )


def _recovery_snapshot_id(params: RecoverySnapshotInput, timestamp: str) -> str:
    return _make_recovery_snapshot_id(
        params=MakeRecoverySnapshotIdParams(
            timestamp=timestamp,
            session_id=params.session_id,
            request_id=params.request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            source=params.source,
            status=params.status,
            user_prompt=params.user_prompt,
            response_text=params.response_text,
        )
    )


def _compression_snapshot_id(params: CompressionSnapshotInput, timestamp: str) -> str:
    return _snapshot_id(
        {
            "created_at": timestamp,
            "session_id": params.session_id,
            "turn_id": params.turn_id,
            "role": params.role,
            "source": params.source,
            "request_id": params.request_id,
            "run_id": params.run_id,
            "task_id": params.task_id,
            "content_hash": _content_hash(params.content),
        }
    )
