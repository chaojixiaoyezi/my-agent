# LLM: Owner policy readers make permissions/quota/retention inspectable without enforcing new gates.
# 模块用途: 读取 owner_home 下的权限、配额和保留策略，并提供轻量磁盘用量统计；这里只报告，不阻断任务。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths


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


def read_owner_policy_bundle(home: MyAgentHomePaths) -> OwnerPolicyBundle:
    return OwnerPolicyBundle(
        permissions=_read_json_object(home.owner_permissions_json),
        quota=_read_json_object(home.owner_quota_json),
        retention=_read_json_object(home.owner_retention_json),
        skill_policy=_read_json_object(home.owner_skill_policy_json),
        tool_policy=_read_json_object(home.owner_tool_policy_json),
    )


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


__all__ = ["OwnerDiskUsage", "OwnerPolicyBundle", "owner_disk_usage", "read_owner_policy_bundle"]
