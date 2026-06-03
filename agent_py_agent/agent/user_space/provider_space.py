
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common.path_segments import safe_path_segment
from .provider_trash import (
    ProviderTrashRequest,
    ProviderTrashResult,
    move_to_space_trash,
    provider_audit_log_path,
    provider_destructive_actions_use_trash_from_agent_config,
    provider_trash_retention_days_from_agent_config,
    purge_provider_trash,
    record_provider_space_event,
    trash_target_for,
)

_SPACE_DIRS = (
    "tools",
    "skills",
    "role_templates",
    "workflows",
    "downloads",
    "cache",
    "tmp",
    "trash",
    "workspace",
    "memory",
    "data",
    "sessions",
)


@dataclass(frozen=True)
class ProviderRootPaths:
    home: Path
    provider: str
    provider_dir: Path
    provider_config: Path
    users_dir: Path
    groups_dir: Path


@dataclass(frozen=True)
class ProviderSpaceIdentity:
    provider: str
    space_type: str
    space_id: str


@dataclass(frozen=True)
class ProviderSpacePaths:
    home: Path
    identity: ProviderSpaceIdentity
    root_dir: Path
    space_config: Path
    tools_dir: Path
    skills_dir: Path
    role_templates_dir: Path
    workflows_dir: Path
    downloads_dir: Path
    cache_dir: Path
    tmp_dir: Path
    trash_dir: Path
    workspace_dir: Path
    memory_dir: Path
    data_dir: Path
    sessions_dir: Path


@dataclass(frozen=True)
class ProviderSpaceQuota:
    max_storage_mb: int = 2048
    max_download_file_mb: int = 200


@dataclass(frozen=True)
class ProviderSpaceQuotaStatus:
    max_storage_mb: int
    max_download_file_mb: int
    used_bytes: int
    used_storage_mb: float
    over_limit: bool


def provider_root_paths(home: str | Path, provider: str) -> ProviderRootPaths:
    root = Path(home)
    provider_id = safe_path_segment(provider)
    provider_dir = root / "providers" / provider_id
    return ProviderRootPaths(
        home=root,
        provider=provider_id,
        provider_dir=provider_dir,
        provider_config=provider_dir / "provider.yaml",
        users_dir=provider_dir / "users",
        groups_dir=provider_dir / "groups",
    )


def ensure_provider_root(home: str | Path, provider: str) -> ProviderRootPaths:
    paths = provider_root_paths(home, provider)
    paths.provider_dir.mkdir(parents=True, exist_ok=True)
    if not paths.provider_config.exists():
        paths.provider_config.write_text(f'provider: "{paths.provider}"\nenabled: true\n', encoding="utf-8")
    return paths


def provider_space_paths(home: str | Path, identity: ProviderSpaceIdentity) -> ProviderSpacePaths:
    normalized = _normalize_identity(identity)
    root = provider_root_paths(home, normalized.provider)
    bucket = "users" if normalized.space_type == "user" else "groups"
    space_root = root.provider_dir / bucket / normalized.space_id
    return ProviderSpacePaths(
        home=root.home,
        identity=normalized,
        root_dir=space_root,
        space_config=space_root / "space.yaml",
        tools_dir=space_root / "tools",
        skills_dir=space_root / "skills",
        role_templates_dir=space_root / "role_templates",
        workflows_dir=space_root / "workflows",
        downloads_dir=space_root / "downloads",
        cache_dir=space_root / "cache",
        tmp_dir=space_root / "tmp",
        trash_dir=space_root / "trash",
        workspace_dir=space_root / "workspace",
        memory_dir=space_root / "memory",
        data_dir=space_root / "data",
        sessions_dir=space_root / "sessions",
    )


def ensure_provider_space(home: str | Path, identity: ProviderSpaceIdentity) -> ProviderSpacePaths:
    ensure_provider_root(home, identity.provider)
    paths = provider_space_paths(home, identity)
    paths.root_dir.mkdir(parents=True, exist_ok=True)
    for dirname in _SPACE_DIRS:
        (paths.root_dir / dirname).mkdir(parents=True, exist_ok=True)
    if not paths.space_config.exists():
        _write_space_config(paths)
    return paths


def can_manage_group_space(role: str) -> bool:
    return str(role).strip().lower() in {"owner", "admin", "group_owner", "group_admin"}


def path_is_within_provider_space(path: str | Path, paths: ProviderSpacePaths) -> bool:
    try:
        Path(path).resolve().relative_to(paths.root_dir.resolve())
        return True
    except ValueError:
        return False


def quota_status(paths: ProviderSpacePaths, quota: ProviderSpaceQuota) -> ProviderSpaceQuotaStatus:
    used = _space_usage_bytes(paths.root_dir)
    max_bytes = max(0, int(quota.max_storage_mb)) * 1024 * 1024
    return ProviderSpaceQuotaStatus(
        max_storage_mb=int(quota.max_storage_mb),
        max_download_file_mb=int(quota.max_download_file_mb),
        used_bytes=used,
        used_storage_mb=used / 1024 / 1024,
        over_limit=bool(max_bytes and used > max_bytes),
    )


def provider_quota_from_agent_config(config: object) -> ProviderSpaceQuota:
    return ProviderSpaceQuota(
        max_storage_mb=int(getattr(config, "provider_space_default_max_storage_mb", 2048)),
        max_download_file_mb=int(getattr(config, "provider_space_max_download_file_mb", 200)),
    )


def _normalize_identity(identity: ProviderSpaceIdentity) -> ProviderSpaceIdentity:
    space_type = str(identity.space_type).strip().lower()
    if space_type not in {"user", "group"}:
        raise ValueError(f"unsupported provider space type: {identity.space_type!r}")
    return ProviderSpaceIdentity(
        provider=safe_path_segment(identity.provider),
        space_type=space_type,
        space_id=safe_path_segment(identity.space_id),
    )


def _space_usage_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for item in root.rglob("*"):
        total += _file_size_or_zero(item)
    return total


def _file_size_or_zero(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


def _write_space_config(paths: ProviderSpacePaths) -> None:
    text = (
        f'provider: "{paths.identity.provider}"\n'
        f'space_type: "{paths.identity.space_type}"\n'
        f'space_id: "{paths.identity.space_id}"\n'
    )
    paths.space_config.write_text(text, encoding="utf-8")
