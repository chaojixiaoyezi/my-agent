
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class LegacyUserPaths:
    """Path bundle for the old data/users layout.

    New runtime code should prefer owner-home paths from `owner_resolver` and
    `runtime_paths`. This type exists so migration/fallback code can name the
    legacy layout honestly.
    """

    user_id: str
    root_dir: Path
    memory_path: Path
    subagent_workspace: Path
    gateway_workspace: Path
    sessions_dir: Path
    local_store_path: Path
    local_store_files_dir: Path
    local_store_events_path: Path


def get_legacy_user_paths(user_id: str, base_dir: Path | str) -> LegacyUserPaths:
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    root_dir = base_dir / user_id
    return LegacyUserPaths(
        user_id=user_id,
        root_dir=root_dir,
        memory_path=root_dir / "memory.jsonl",
        subagent_workspace=root_dir / "subagents",
        gateway_workspace=root_dir / "gateway",
        sessions_dir=root_dir / "sessions",
        local_store_path=root_dir / "local_store" / "local.db",
        local_store_files_dir=root_dir / "local_store" / "files",
        local_store_events_path=root_dir / "local_store" / "events.jsonl",
    )


def get_legacy_admin_paths(base_dir: Path | str) -> LegacyUserPaths:
    return get_legacy_user_paths("admin", base_dir)


__all__ = ["LegacyUserPaths", "get_legacy_admin_paths", "get_legacy_user_paths"]
