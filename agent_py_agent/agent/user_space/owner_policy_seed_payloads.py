# LLM: Owner policy seed payloads define fresh-install defaults for every owner home.
# 模块用途: 提供 permissions/quota/retention/skill/tool policy 的初始 JSON，避免 owner_resolver 继续膨胀。

from __future__ import annotations


# LLM: default_permissions_payload is intentionally capability-shaped rather than task-shaped.
# 函数用途: 生成新 owner 的默认权限边界，作为后续 provider/user/group 权限解析的起点。
def default_permissions_payload() -> dict[str, object]:
    return {
        "schema_version": "permissions.v1",
        "filesystem": {"access_mode": "workspace-write", "dangerous_paths": ["/", "/etc", "/System", "~/.ssh"]},
        "network": {"enabled": True},
        "shell": {"inherits_parent": True},
        "subagents": {"inheritance": "parent_capped"},
    }


# LLM: default_quota_payload keeps count and storage limits separate from permission decisions.
# 函数用途: 生成新 owner 的资源配额默认值；权限判断仍由 permissions/policy 层决定。
def default_quota_payload() -> dict[str, object]:
    return {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }


# LLM: default_retention_payload documents default cleanup windows for owner-scoped memory and work data.
# 函数用途: 生成新 owner 的保留策略默认值，供 doctor/retention 后续统一读取。
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


# LLM: default_skill_policy_payload gives each owner a private/shared/builtin skill allow policy.
# 函数用途: 生成新 owner 的 skill 来源、禁用项和版本 pin 默认配置。
def default_skill_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "skill-policy.v1",
        "enabled_sources": ["owner", "workspace", "shared", "builtin"],
        "enabled_shared_skills": [],
        "disabled_skills": [],
        "pin_versions": {},
    }


# LLM: default_tool_policy_payload mirrors skill policy for tools without granting extra runtime privilege.
# 函数用途: 生成新 owner 的 tool 来源、禁用项和版本 pin 默认配置。
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
