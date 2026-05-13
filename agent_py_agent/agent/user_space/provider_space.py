# LLM: Provider spaces isolate external connector users/groups from the owner home and from each other.
# 模块用途: 管理 QQ/飞书等外部平台的用户/群空间、配额、trash 和路径边界。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


# LLM: ProviderRootPaths describes a connector root that is created lazily when the provider is enabled.
# 类用途: 保存某个平台根目录和 provider.yaml 的路径，不提前创建用户/群目录。
@dataclass(frozen=True)
class ProviderRootPaths:
    home: Path
    provider: str
    provider_dir: Path
    provider_config: Path
    users_dir: Path
    groups_dir: Path


# LLM: ProviderSpaceIdentity is the explicit scope key for one external user or group.
# 类用途: 描述外部平台、空间类型和稳定用户/群 ID。
@dataclass(frozen=True)
class ProviderSpaceIdentity:
    provider: str
    space_type: str
    space_id: str


# LLM: ProviderSpacePaths is the boundary object handed to tools, skills, and cleanup code.
# 类用途: 保存某个外部用户或群空间内可读写目录的位置。
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


# LLM: ProviderSpaceQuota keeps external storage limits config-backed in MiB.
# 类用途: 描述外部用户/群空间的容量上限和单文件下载上限。
@dataclass(frozen=True)
class ProviderSpaceQuota:
    max_storage_mb: int = 2048
    max_download_file_mb: int = 200


# LLM: ProviderSpaceQuotaStatus is cheap status data for doctor/UI without reading file contents.
# 类用途: 返回某个外部用户/群空间当前占用和是否超额。
@dataclass(frozen=True)
class ProviderSpaceQuotaStatus:
    max_storage_mb: int
    max_download_file_mb: int
    used_bytes: int
    used_storage_mb: float
    over_limit: bool


# LLM: provider_root_paths returns provider root paths without creating directories.
# 函数用途: 根据家目录和平台名计算 provider 根路径。
def provider_root_paths(home: str | Path, provider: str) -> ProviderRootPaths:
    root = Path(home)
    provider_id = _safe_segment(provider)
    provider_dir = root / "providers" / provider_id
    return ProviderRootPaths(
        home=root,
        provider=provider_id,
        provider_dir=provider_dir,
        provider_config=provider_dir / "provider.yaml",
        users_dir=provider_dir / "users",
        groups_dir=provider_dir / "groups",
    )


# LLM: ensure_provider_root lazily creates only provider-level metadata.
# 函数用途: 创建平台根目录和 provider.yaml，但不提前创建 users/groups。
def ensure_provider_root(home: str | Path, provider: str) -> ProviderRootPaths:
    paths = provider_root_paths(home, provider)
    paths.provider_dir.mkdir(parents=True, exist_ok=True)
    if not paths.provider_config.exists():
        paths.provider_config.write_text(f'provider: "{paths.provider}"\nenabled: true\n', encoding="utf-8")
    return paths


# LLM: provider_space_paths maps one provider identity to its isolated filesystem scope.
# 函数用途: 计算某个外部用户或群空间内所有标准目录路径。
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


# LLM: ensure_provider_space materializes only the requested user/group scope.
# 函数用途: 创建指定外部用户/群的隔离空间和可自定义 tools/skills/templates/workflows 目录。
def ensure_provider_space(home: str | Path, identity: ProviderSpaceIdentity) -> ProviderSpacePaths:
    ensure_provider_root(home, identity.provider)
    paths = provider_space_paths(home, identity)
    paths.root_dir.mkdir(parents=True, exist_ok=True)
    for dirname in _SPACE_DIRS:
        (paths.root_dir / dirname).mkdir(parents=True, exist_ok=True)
    if not paths.space_config.exists():
        _write_space_config(paths)
    return paths


# LLM: can_manage_group_space is intentionally narrow so members cannot destroy group-owned state.
# 函数用途: 判断某个群角色是否能管理本群空间里的破坏性操作。
def can_manage_group_space(role: str) -> bool:
    return str(role).strip().lower() in {"owner", "admin", "group_owner", "group_admin"}


# LLM: path_is_within_provider_space guards external user/group writes against owner home escape.
# 函数用途: 判断目标路径是否仍在当前 provider user/group 空间内。
def path_is_within_provider_space(path: str | Path, paths: ProviderSpacePaths) -> bool:
    try:
        Path(path).resolve().relative_to(paths.root_dir.resolve())
        return True
    except ValueError:
        return False


# LLM: quota_status computes scoped usage without opening file contents.
# 函数用途: 统计 provider 空间占用并按配置返回是否超过容量上限。
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


# LLM: provider_quota_from_agent_config keeps provider-space quotas controlled by backend config.
# 函数用途: 从 AgentConfig 或兼容对象中生成外部用户/群空间配额对象。
def provider_quota_from_agent_config(config: object) -> ProviderSpaceQuota:
    return ProviderSpaceQuota(
        max_storage_mb=int(getattr(config, "provider_space_default_max_storage_mb", 2048)),
        max_download_file_mb=int(getattr(config, "provider_space_max_download_file_mb", 200)),
    )


# LLM: _normalize_identity sanitizes provider/user/group names before they become path segments.
# 函数用途: 规范化外部空间身份，并拒绝未知空间类型。
def _normalize_identity(identity: ProviderSpaceIdentity) -> ProviderSpaceIdentity:
    space_type = str(identity.space_type).strip().lower()
    if space_type not in {"user", "group"}:
        raise ValueError(f"unsupported provider space type: {identity.space_type!r}")
    return ProviderSpaceIdentity(
        provider=_safe_segment(identity.provider),
        space_type=space_type,
        space_id=_safe_segment(identity.space_id),
    )


# LLM: _safe_segment removes path separators and glob-ish surprises from provider IDs.
# 函数用途: 把 provider、user_id、group_id 转成安全的单级目录名。
def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in text)
    result = result.strip(".-_/")
    return result or "unknown"


# LLM: _space_usage_bytes walks one scoped tree and tolerates concurrent file churn.
# 函数用途: 统计目录树中文件大小，遇到并发删除或权限错误时跳过。
def _space_usage_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for item in root.rglob("*"):
        total += _file_size_or_zero(item)
    return total


# LLM: _file_size_or_zero hides concurrent file churn from quota scans.
# 函数用途: 返回文件大小；目录、权限错误或并发删除时返回 0。
def _file_size_or_zero(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


# LLM: _write_space_config records the scope so humans and future tools can inspect ownership.
# 函数用途: 首次创建外部用户/群空间时写入轻量 space.yaml。
def _write_space_config(paths: ProviderSpacePaths) -> None:
    text = (
        f'provider: "{paths.identity.provider}"\n'
        f'space_type: "{paths.identity.space_type}"\n'
        f'space_id: "{paths.identity.space_id}"\n'
    )
    paths.space_config.write_text(text, encoding="utf-8")
