
from __future__ import annotations

"""public API for recovery and compression snapshot writes.

新手说明:
本包负责 compression hook 注册和 snapshot 写入入口；具体构造与文件写入在内部模块。
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ._builders import (
    CompressionSnapshotBuildParams,
    MakeRecoverySnapshotIdParams,
    RecoverySnapshotBuildParams,
    _build_compression_snapshot,
    _build_recovery_snapshot,
    _make_recovery_snapshot_id,
)
from ._helpers import (
    SNAPSHOT_PREVIEW_LIMITS,
    _content_hash,
    _dedupe_texts,
    _normalize_archive_level,
    _participants,
    _preview,
    _snapshot_id,
    _stable_json,
    _tool_snapshot,
)
from ._types import (
    CompressionHook,
    CompressionHookResult,
    CompressionSnapshotInput,
    RecoverySnapshotInput,
    RecoverySnapshotResult,
    _compression_hooks,
)
from ._writers import _write_compression_snapshot_impl, _write_recovery_snapshot_impl


def register_compression_hook(hook: CompressionHook) -> None:
    """Register a callback that runs before every compression."""
    _compression_hooks.append(hook)


def clear_compression_hooks() -> None:
    """Remove all registered compression hooks."""
    _compression_hooks.clear()


def on_before_compression(
    root: str | Path,
    *,
    params: CompressionSnapshotInput | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    role: str | None = None,
    content: str | None = None,
    archive_level: int = 3,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    source: str = "compression",
    backend: str = "",
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    content_paths: Iterable[str] | None = None,
    task_refs: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    created_at: str | None = None,
) -> CompressionHookResult:
    """Run registered hooks and write the mandatory pre-compression snapshot."""
    params = params or CompressionSnapshotInput(
        session_id=str(session_id),
        turn_id=str(turn_id),
        role=str(role),
        content=str(content),
        archive_level=archive_level,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        source=source,
        backend=backend,
        tool_calls=tool_calls,
        content_paths=content_paths,
        task_refs=task_refs,
        next_actions=next_actions,
        created_at=created_at,
    )
    for hook in _compression_hooks:
        hook(session_id=params.session_id, turn_id=params.turn_id, archive_level=params.archive_level)
    return write_compression_snapshot(root, params=params)


def write_recovery_snapshot(
    root: str | Path,
    *,
    params: RecoverySnapshotInput | None = None,
    session_id: str | None = None,
    user_prompt: str | None = None,
    response_text: str | None = None,
    backend: str | None = None,
    source: str | None = None,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    status: str = "ok",
    error_code: str = "",
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    task_refs: Iterable[str] | None = None,
    content_paths: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    archive_level: int = 3,
    created_at: str | None = None,
) -> RecoverySnapshotResult:
    """Write one minimal hook snapshot and return a non-throwing result."""
    params = params or RecoverySnapshotInput(
        session_id=str(session_id),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        source=str(source),
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        status=status,
        error_code=error_code,
        tool_calls=tool_calls,
        task_refs=task_refs,
        content_paths=content_paths,
        next_actions=next_actions,
        archive_level=archive_level,
        created_at=created_at,
    )
    return _write_recovery_snapshot_impl(root, params)


def write_compression_snapshot(
    root: str | Path,
    *,
    params: CompressionSnapshotInput | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    role: str | None = None,
    content: str | None = None,
    archive_level: int = 3,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    source: str = "compression",
    backend: str = "",
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    content_paths: Iterable[str] | None = None,
    task_refs: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    created_at: str | None = None,
) -> CompressionHookResult:
    """Write authoritative and searchable pre-compression snapshots."""
    params = params or CompressionSnapshotInput(
        session_id=str(session_id),
        turn_id=str(turn_id),
        role=str(role),
        content=str(content),
        archive_level=archive_level,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        source=source,
        backend=backend,
        tool_calls=tool_calls,
        content_paths=content_paths,
        task_refs=task_refs,
        next_actions=next_actions,
        created_at=created_at,
    )
    return _write_compression_snapshot_impl(root, params)


__all__ = [
    "CompressionHook",
    "CompressionHookResult",
    "CompressionSnapshotInput",
    "RecoverySnapshotInput",
    "RecoverySnapshotResult",
    "SNAPSHOT_PREVIEW_LIMITS",
    "_compression_hooks",
    "_content_hash",
    "_dedupe_texts",
    "_normalize_archive_level",
    "_participants",
    "_preview",
    "_snapshot_id",
    "_stable_json",
    "_tool_snapshot",
    "clear_compression_hooks",
    "on_before_compression",
    "register_compression_hook",
    "write_compression_snapshot",
    "write_recovery_snapshot",
]
