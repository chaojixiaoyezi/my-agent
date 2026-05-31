# LLM: Owner policy readers make permissions/quota/retention inspectable without enforcing new gates.
# 模块用途: 读取 owner_home 下的权限、配额和保留策略，并提供轻量磁盘用量统计；这里只报告，不阻断任务。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths
from .temporary_grants import list_temporary_grants

_NETWORK_TOOL_NAMES = frozenset({"web_search", "web_fetch", "http_request"})


@dataclass(frozen=True)
class OwnerPolicyBundle:
    permissions: dict[str, Any]
    quota: dict[str, Any]
    retention: dict[str, Any]
    skill_policy: dict[str, Any]
    tool_policy: dict[str, Any]


@dataclass(frozen=True)
class OwnerDiskUsage:
    total_bytes: int
    by_root: dict[str, int]


@dataclass(frozen=True)
class EffectiveOwnerPolicy:
    owner_id: str
    owner_home: str
    filesystem_access_mode: str
    dangerous_paths: tuple[str, ...]
    shell_access_mode: str
    network_enabled: bool
    max_active_agents: int
    max_subagents: int
    max_depth: int
    max_disk_mb: int
    enabled_tool_sources: tuple[str, ...]
    disabled_tools: tuple[str, ...]
    enabled_skill_sources: tuple[str, ...]
    disabled_skills: tuple[str, ...]
    active_grants: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "effective-owner-policy.v1",
            "owner_id": self.owner_id,
            "owner_home": self.owner_home,
            "filesystem": {
                "access_mode": self.filesystem_access_mode,
                "dangerous_paths": list(self.dangerous_paths),
            },
            "shell": {"access_mode": self.shell_access_mode},
            "network": {"enabled": self.network_enabled},
            "quota": {
                "max_active_agents": self.max_active_agents,
                "max_subagents": self.max_subagents,
                "max_depth": self.max_depth,
                "max_disk_mb": self.max_disk_mb,
            },
            "tools": {
                "enabled_sources": list(self.enabled_tool_sources),
                "disabled_tools": list(self.disabled_tools),
            },
            "skills": {
                "enabled_sources": list(self.enabled_skill_sources),
                "disabled_skills": list(self.disabled_skills),
            },
            "active_grants": list(self.active_grants),
        }


def read_owner_policy_bundle(home: MyAgentHomePaths) -> OwnerPolicyBundle:
    return OwnerPolicyBundle(
        permissions=_read_json_object(home.owner_permissions_json),
        quota=_read_json_object(home.owner_quota_json),
        retention=_read_json_object(home.owner_retention_json),
        skill_policy=_read_json_object(home.owner_skill_policy_json),
        tool_policy=_read_json_object(home.owner_tool_policy_json),
    )


def resolve_effective_owner_policy(
    home: MyAgentHomePaths,
    *,
    parent_policy: EffectiveOwnerPolicy | None = None,
) -> EffectiveOwnerPolicy:
    bundle = read_owner_policy_bundle(home)
    permissions = bundle.permissions
    quota = bundle.quota
    tool_policy = bundle.tool_policy
    skill_policy = bundle.skill_policy

    filesystem = _dict_value(permissions.get("filesystem"))
    shell = _dict_value(permissions.get("shell"))
    network_enabled = _effective_network_enabled(permissions, parent_policy)
    disabled_tools = _effective_disabled_tools(tool_policy, network_enabled, parent_policy)

    return EffectiveOwnerPolicy(
        owner_id=str(getattr(home, "owner_id", "") or "local:main:main"),
        owner_home=str(getattr(home, "owner_home_dir", "") or home.root),
        filesystem_access_mode=_child_capped_access_mode(
            str(filesystem.get("access_mode") or "workspace-write"),
            parent_policy.filesystem_access_mode if parent_policy else "",
        ),
        dangerous_paths=_string_tuple(filesystem.get("dangerous_paths")),
        shell_access_mode=_child_capped_access_mode(
            str(shell.get("access_mode") or filesystem.get("access_mode") or "workspace-write"),
            parent_policy.shell_access_mode if parent_policy else "",
        ),
        network_enabled=network_enabled if parent_policy is None else bool(network_enabled and parent_policy.network_enabled),
        max_active_agents=_positive_int(quota.get("max_active_agents"), 1000),
        max_subagents=_child_capped_limit(
            _positive_int(quota.get("max_subagents"), 50),
            parent_policy.max_subagents if parent_policy else 0,
        ),
        max_depth=_child_capped_limit(
            _positive_int(quota.get("max_depth"), 4),
            parent_policy.max_depth if parent_policy else 0,
        ),
        max_disk_mb=_positive_int(quota.get("max_disk_mb"), 102400),
        enabled_tool_sources=_string_tuple(tool_policy.get("enabled_sources")),
        disabled_tools=tuple(sorted(disabled_tools)),
        enabled_skill_sources=_string_tuple(skill_policy.get("enabled_sources")),
        disabled_skills=_string_tuple(skill_policy.get("disabled_skills")),
        active_grants=tuple(_grant_payload(grant) for grant in list_temporary_grants(home, status="active")),
    )


def _effective_network_enabled(
    permissions: dict[str, Any],
    parent_policy: EffectiveOwnerPolicy | None,
) -> bool:
    network = _dict_value(permissions.get("network"))
    enabled = bool(network.get("enabled", True))
    return enabled if parent_policy is None else bool(enabled and parent_policy.network_enabled)


def _effective_disabled_tools(
    tool_policy: dict[str, Any],
    network_enabled: bool,
    parent_policy: EffectiveOwnerPolicy | None,
) -> set[str]:
    disabled_tools = set(_string_tuple(tool_policy.get("disabled_tools")))
    if not network_enabled:
        disabled_tools.update(_NETWORK_TOOL_NAMES)
    if parent_policy:
        disabled_tools.update(parent_policy.disabled_tools)
    return disabled_tools


def owner_disk_usage(home: MyAgentHomePaths) -> OwnerDiskUsage:
    roots = (
        home.owner_memory_dir,
        home.owner_workspace_dir,
        home.owner_artifacts_dir,
        home.owner_cache_dir,
        home.owner_tmp_dir,
        home.owner_logs_dir,
    )
    by_root = {str(root): _directory_size(root) for root in roots}
    return OwnerDiskUsage(total_bytes=sum(by_root.values()), by_root=by_root)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raw = str(value or "").strip()
    return (raw,) if raw else ()


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _child_capped_limit(value: int, parent: int) -> int:
    if parent <= 0:
        return value
    return min(value, parent)


def _child_capped_access_mode(value: str, parent: str) -> str:
    ranks = {"restricted": 0, "workspace-write": 1, "full-access": 2}
    normalized = value if value in ranks else "workspace-write"
    parent_normalized = parent if parent in ranks else ""
    if not parent_normalized:
        return normalized
    if ranks[normalized] > ranks[parent_normalized]:
        return parent_normalized
    if parent_normalized == "full-access" and normalized == "full-access":
        return "workspace-write"
    return normalized


def _grant_payload(grant) -> dict[str, str]:
    return {
        "grant_id": str(grant.grant_id),
        "granted_to": str(grant.granted_to),
        "capability": str(grant.capability),
        "path_prefix": str(grant.path_prefix),
        "expires_at": str(grant.expires_at),
    }


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        total += _file_size(path)
    return total


def _file_size(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


__all__ = [
    "EffectiveOwnerPolicy",
    "OwnerDiskUsage",
    "OwnerPolicyBundle",
    "owner_disk_usage",
    "read_owner_policy_bundle",
    "resolve_effective_owner_policy",
]
