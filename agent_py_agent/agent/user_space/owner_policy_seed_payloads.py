
from __future__ import annotations


def default_permissions_payload() -> dict[str, object]:
    return {
        "schema_version": "permissions.v1",
        "filesystem": {"access_mode": "workspace-write", "dangerous_paths": ["/", "/etc", "/System", "~/.ssh"]},
        "network": {"enabled": True},
        "shell": {"inherits_parent": True},
        "subagents": {"inheritance": "parent_capped"},
    }


def default_quota_payload() -> dict[str, object]:
    return {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }


def default_retention_payload() -> dict[str, object]:
    return {
        "schema_version": "retention.v1",
        "raw_days": 90,
        "daily_days": 365,
        "hooks_days": 180,
        "compact_days": 365,
        "task_completed_days": 365,
        "subagent_scratch_days": 30,
        "trash_days": 30,
    }


def default_skill_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "skill-policy.v1",
        "enabled_sources": ["owner", "workspace", "shared", "builtin"],
        "enabled_shared_skills": [],
        "disabled_skills": [],
        "pin_versions": {},
    }


def default_tool_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "tool-policy.v1",
        "enabled_sources": ["builtin", "owner", "workspace", "shared"],
        "disabled_tools": [],
        "pin_versions": {},
    }


__all__ = [
    "default_permissions_payload",
    "default_quota_payload",
    "default_retention_payload",
    "default_skill_policy_payload",
    "default_tool_policy_payload",
]
