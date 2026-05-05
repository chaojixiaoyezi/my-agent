from __future__ import annotations

"""LLM: public facade for recovery and compression snapshot writes.

新手说明:
这个包的公开导入路径保持不变；具体构造和写入实现拆到内部模块，
这样 snapshots 入口只负责 hook 注册、参数兼容和 re-export。
"""

from pathlib import Path

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
    **kwargs,
) -> CompressionHookResult:
    """Run registered hooks and write the mandatory pre-compression snapshot."""
    if params is None:
        params = CompressionSnapshotInput(**kwargs)
    for hook in _compression_hooks:
        hook(session_id=params.session_id, turn_id=params.turn_id, archive_level=params.archive_level)
    return write_compression_snapshot(root, params=params)


def write_recovery_snapshot(
    root: str | Path,
    *,
    params: RecoverySnapshotInput | None = None,
    **kwargs,
) -> RecoverySnapshotResult:
    """Write one minimal hook snapshot and return a non-throwing result."""
    if params is None:
        params = RecoverySnapshotInput(**kwargs)
    return _write_recovery_snapshot_impl(root, params)


def write_compression_snapshot(
    root: str | Path,
    *,
    params: CompressionSnapshotInput | None = None,
    **kwargs,
) -> CompressionHookResult:
    """Write authoritative and searchable pre-compression snapshots."""
    if params is None:
        params = CompressionSnapshotInput(**kwargs)
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
