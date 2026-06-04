
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimePathResolution:
    """Structured result for current owner-home runtime path selection."""

    paths: dict[str, Path]
    reason: str
    explicit_overrides: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OwnerRuntimePathInputs:
    config: Any
    home: Any
    owner_home: Path
    root: Path
    runtime_root: Path


def resolve_runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> RuntimePathResolution:
    if not getattr(home, "owner_home_dir", None):
        raise ValueError("owner home is required for runtime paths")
    paths, overrides = _owner_runtime_paths(config, home, root=root)
    return RuntimePathResolution(
        paths=paths,
        reason="owner_home_runtime",
        explicit_overrides=overrides,
    )


def runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> dict[str, Path]:
    return resolve_runtime_paths_for_agent(config, root, home).paths


def apply_runtime_paths_to_config(config: Any, resolution: RuntimePathResolution) -> None:
    for field_name, path in resolution.paths.items():
        if hasattr(config, field_name):
            setattr(config, field_name, str(path))


def _owner_runtime_paths(config: Any, home: Any, *, root: Path) -> tuple[dict[str, Path], tuple[str, ...]]:
    owner_home = Path(home.owner_home_dir)
    owner_workspace = Path(getattr(home, "owner_workspace_dir", owner_home / "workspace"))
    runtime_root = owner_workspace / "runtime" / "workspaces" / _workspace_scope_id(root)
    inputs = _OwnerRuntimePathInputs(config=config, home=home, owner_home=owner_home, root=root, runtime_root=runtime_root)
    paths = {
        **_owner_memory_and_session_paths(inputs),
        **_owner_workspace_runtime_paths(inputs),
    }
    overrides = tuple(field_name for field_name in paths if str(getattr(config, field_name, "") or ""))
    return paths, overrides


def _owner_memory_and_session_paths(inputs: _OwnerRuntimePathInputs) -> dict[str, Path]:
    config = inputs.config
    home = inputs.home
    owner_home = inputs.owner_home
    root = inputs.root
    memory_path = Path(getattr(home, "owner_memory_long_term_dir", owner_home / "memory" / "long_term")) / "memory.jsonl"
    return {
        "memory_path": memory_path,
        "session_workspace": _configured_or_default(
            config,
            root,
            "session_workspace",
            Path(getattr(home, "owner_sessions_dir", owner_home / "sessions")),
        ),
        "audit_log_path": _configured_or_default(
            config,
            root,
            "audit_log_path",
            Path(getattr(home, "owner_logs_dir", owner_home / "logs")) / "audit",
        ),
        "model_speed_profile_path": _configured_or_default(
            config,
            root,
            "model_speed_profile_path",
            Path(getattr(home, "owner_cache_dir", owner_home / "cache")) / "model_speed_profile.json",
        ),
    }


def _owner_workspace_runtime_paths(inputs: _OwnerRuntimePathInputs) -> dict[str, Path]:
    config = inputs.config
    owner_home = inputs.owner_home
    root = inputs.root
    runtime_root = inputs.runtime_root
    local_store_dir = runtime_root / "local_store"
    return {
        "local_store_path": _configured_or_default(config, root, "local_store_path", local_store_dir / "local.db"),
        "local_store_files_dir": _configured_or_default(config, root, "local_store_files_dir", local_store_dir / "files"),
        "local_store_events_path": _configured_or_default(
            config,
            root,
            "local_store_events_path",
            local_store_dir / "events.jsonl",
        ),
        "subagent_workspace": _configured_or_default(config, root, "subagent_workspace", runtime_root / "subagents"),
        "gateway_workspace": _configured_or_default(config, root, "gateway_workspace", runtime_root / "gateway"),
        "adapter_workspace": _configured_or_default(config, root, "adapter_workspace", runtime_root / "adapters" / "file"),
        "conversation_workspace": _configured_or_default(
            config,
            root,
            "conversation_workspace",
            runtime_root / "conversations",
        ),
        "collaboration_workspace": _configured_or_default(
            config,
            root,
            "collaboration_workspace",
            runtime_root / "collaboration",
        ),
        "notification_store_path": _configured_or_default(
            config,
            root,
            "notification_store_path",
            runtime_root / "notifications",
        ),
    }


def _configured_or_default(config: Any, root: Path, field_name: str, owner_default: Path) -> Path:
    raw = str(getattr(config, field_name, "") or "")
    if not raw:
        return owner_default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(root) / path


def _workspace_scope_id(root: Path) -> str:
    resolved = str(Path(root).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    name = Path(resolved).name or "workspace"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name).strip("-_")
    return f"{safe or 'workspace'}-{digest}"


__all__ = [
    "RuntimePathResolution",
    "apply_runtime_paths_to_config",
    "resolve_runtime_paths_for_agent",
    "runtime_paths_for_agent",
]
