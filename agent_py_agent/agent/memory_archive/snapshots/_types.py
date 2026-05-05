"""Public dataclasses and hook registry for memory snapshot writing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class CompressionHook(Protocol):
    """Protocol for external systems that need to run before compression."""

    def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
        """Called before compression proceeds. Raise to block compression."""
        ...


_compression_hooks: list[CompressionHook] = []


@dataclass(frozen=True)
class CompressionSnapshotInput:
    """Input bundle for write_compression_snapshot and on_before_compression."""
    session_id: str
    turn_id: str
    role: str
    content: str
    archive_level: int = 3
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "compression"
    backend: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    content_paths: Iterable[str] | None = None
    task_refs: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class RecoverySnapshotInput:
    """Input bundle for write_recovery_snapshot."""
    session_id: str
    user_prompt: str
    response_text: str
    backend: str
    source: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    status: str = "ok"
    error_code: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    task_refs: Iterable[str] | None = None
    content_paths: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    archive_level: int = 3
    created_at: str | None = None


@dataclass(frozen=True)
class RecoverySnapshotResult:
    """Result returned after a best-effort recovery snapshot write."""
    ok: bool
    snapshot_id: str = ""
    path: str = ""
    token_estimate: int = 0
    error: str = ""


@dataclass(frozen=True)
class CompressionHookResult:
    """Result of the pre-compression hook that must succeed before compression proceeds."""
    snapshot_id: str
    hook_path: str
    snapshot_file_path: str
    token_estimate: int
    archive_level: int
