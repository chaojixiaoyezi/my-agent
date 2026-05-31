# LLM: V2 home layout extensions keep owner/shared/system paths out of the legacy resolver.
# 模块用途: 提供 my-agent-home.v2 的路径字段、目录清单和默认种子配置。

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME_SCHEMA_VERSION = "my-agent-home.v2"


def v2_home_path_fields(home: Path) -> dict[str, Path]:
    return {
        **_shared_path_fields(home),
        **_owner_path_fields(home),
        **_identity_path_fields(home),
        **_global_index_path_fields(home),
        **_system_path_fields(home),
    }


def v2_home_directories(paths: Any) -> tuple[Path, ...]:
    return (
        *_shared_directories(paths),
        *_owner_directories(paths),
        *_identity_directories(paths),
        *_system_directories(paths),
    )


def _shared_directories(paths: Any) -> tuple[Path, ...]:
    return (
        paths.shared_builtin_dir,
        paths.shared_tools_dir,
        paths.shared_skills_dir,
        paths.shared_optional_skills_dir,
        paths.shared_workflows_dir,
        paths.shared_role_templates_dir,
        paths.shared_policy_templates_dir,
        paths.shared_scripts_dir,
        paths.shared_indexes_dir,
    )


def _owner_directories(paths: Any) -> tuple[Path, ...]:
    return (
        paths.local_owners_dir,
        paths.owner_home_dir,
        paths.owner_sessions_dir,
        paths.owner_memory_daily_dir,
        paths.owner_memory_raw_dir,
        paths.owner_memory_hooks_dir,
        paths.owner_memory_lessons_dir,
        paths.owner_memory_routing_dir,
        paths.owner_memory_indexes_dir,
        paths.owner_memory_long_term_dir,
        paths.owner_memory_runtime_refs_dir,
        paths.owner_tasks_dir,
        paths.owner_runs_dir,
        paths.owner_agents_dir,
        paths.owner_compact_dir / "by_task",
        paths.owner_compact_dir / "by_run",
        paths.owner_compact_dir / "by_agent",
        paths.owner_workspace_dir,
        paths.owner_artifacts_dir,
        paths.owner_data_dir,
        paths.owner_logs_dir,
        paths.owner_cache_dir,
        paths.owner_tmp_dir,
        paths.owner_trash_dir,
        paths.owner_capability_requests_dir,
        paths.owner_temporary_grants_dir,
        paths.owner_home_dir / "skills" / ".drafts",
        paths.owner_home_dir / "skills" / ".archive",
        paths.owner_home_dir / "tools" / ".drafts",
        paths.owner_home_dir / "tools" / ".archive",
        paths.owner_home_dir / "workflows",
        paths.owner_home_dir / "role_templates",
    )


def _identity_directories(paths: Any) -> tuple[Path, ...]:
    return (
        paths.identity_dir,
        paths.canonical_users_dir,
        paths.provider_identity_dir,
        paths.global_index_dir,
    )


def _system_directories(paths: Any) -> tuple[Path, ...]:
    return (
        paths.system_config_dir,
        paths.system_audit_dir,
        paths.system_metrics_dir,
        paths.system_doctor_dir,
        paths.system_backups_dir,
        paths.system_migrations_dir,
    )


def v2_seed_files(paths: Any) -> tuple[tuple[Path, str], ...]:
    return (
        (paths.owner_soul_md, "# SOUL\n\n"),
        (paths.owner_user_md, "# USER\n\n"),
        (paths.owner_agents_md, "# AGENTS\n\n"),
        (paths.owner_memory_md, "# Memory\n\n"),
        (paths.owner_memory_hot_md, "# Memory HOT\n\nOwner-specific HOT memory can override or refine root HOT memory.\n"),
        (paths.owner_memory_routing_index_md, "# Owner Memory Routing Index\n\n"),
        (paths.shared_indexes_tools_jsonl, ""),
        (paths.shared_indexes_skills_jsonl, ""),
        (paths.shared_indexes_workflows_jsonl, ""),
        (paths.shared_indexes_role_templates_jsonl, ""),
        (paths.linked_identities_jsonl, ""),
        (paths.global_index_owners_jsonl, ""),
        (paths.global_index_active_tasks_jsonl, ""),
        (paths.global_index_active_runs_jsonl, ""),
        (paths.global_index_active_agents_jsonl, ""),
        (paths.owner_audit_log_jsonl, ""),
    )


def v2_seed_jsons(paths: Any) -> tuple[tuple[Path, dict[str, object]], ...]:
    return (
        (paths.system_schema_version_json, _schema_version_payload()),
        (paths.owner_permissions_json, _default_permissions_payload()),
        (paths.owner_quota_json, _default_quota_payload()),
        (paths.owner_retention_json, _default_retention_payload()),
        (paths.owner_skill_policy_json, _default_skill_policy_payload()),
        (paths.owner_tool_policy_json, _default_tool_policy_payload()),
    )


def _shared_path_fields(home: Path) -> dict[str, Path]:
    shared_dir = home / "shared"
    shared_indexes_dir = shared_dir / "indexes"
    return {
        "shared_dir": shared_dir,
        "shared_builtin_dir": shared_dir / "builtin",
        "shared_tools_dir": shared_dir / "tools",
        "shared_skills_dir": shared_dir / "skills",
        "shared_optional_skills_dir": shared_dir / "optional_skills",
        "shared_workflows_dir": shared_dir / "workflows",
        "shared_role_templates_dir": shared_dir / "role_templates",
        "shared_policy_templates_dir": shared_dir / "policy_templates",
        "shared_scripts_dir": shared_dir / "scripts",
        "shared_indexes_dir": shared_indexes_dir,
        "shared_indexes_tools_jsonl": shared_indexes_dir / "tools.jsonl",
        "shared_indexes_skills_jsonl": shared_indexes_dir / "skills.jsonl",
        "shared_indexes_workflows_jsonl": shared_indexes_dir / "workflows.jsonl",
        "shared_indexes_role_templates_jsonl": shared_indexes_dir / "role_templates.jsonl",
    }


def _owner_path_fields(home: Path) -> dict[str, Path]:
    owners_dir = home / "owners"
    owner_home_dir = owners_dir / "local" / "main"
    owner_memory_dir = owner_home_dir / "memory"
    return {
        "owners_dir": owners_dir,
        "local_owners_dir": owners_dir / "local",
        "owner_home_dir": owner_home_dir,
        "owner_soul_md": owner_home_dir / "SOUL.md",
        "owner_user_md": owner_home_dir / "USER.md",
        "owner_agents_md": owner_home_dir / "AGENTS.md",
        "owner_memory_md": owner_home_dir / "memory.md",
        "owner_memory_hot_md": owner_home_dir / "memory-hot.md",
        "owner_permissions_json": owner_home_dir / "permissions.json",
        "owner_quota_json": owner_home_dir / "quota.json",
        "owner_retention_json": owner_home_dir / "retention.json",
        "owner_skill_policy_json": owner_home_dir / "skill_policy.json",
        "owner_tool_policy_json": owner_home_dir / "tool_policy.json",
        "owner_sessions_dir": owner_home_dir / "sessions",
        "owner_memory_dir": owner_memory_dir,
        "owner_memory_daily_dir": owner_memory_dir / "daily",
        "owner_memory_raw_dir": owner_memory_dir / "raw",
        "owner_memory_hooks_dir": owner_memory_dir / "hooks",
        "owner_memory_lessons_dir": owner_memory_dir / "lessons",
        "owner_memory_routing_dir": owner_memory_dir / "routing",
        "owner_memory_routing_index_md": owner_memory_dir / "routing" / "INDEX.md",
        "owner_memory_indexes_dir": owner_memory_dir / "indexes",
        "owner_memory_long_term_dir": owner_memory_dir / "long_term",
        "owner_memory_runtime_refs_dir": owner_memory_dir / "runtime_refs",
        "owner_tasks_dir": owner_home_dir / "tasks",
        "owner_runs_dir": owner_home_dir / "runs",
        "owner_agents_dir": owner_home_dir / "agents",
        "owner_compact_dir": owner_home_dir / "compact",
        "owner_workspace_dir": owner_home_dir / "workspace",
        "owner_artifacts_dir": owner_home_dir / "artifacts",
        "owner_data_dir": owner_home_dir / "data",
        "owner_logs_dir": owner_home_dir / "logs",
        "owner_cache_dir": owner_home_dir / "cache",
        "owner_tmp_dir": owner_home_dir / "tmp",
        "owner_trash_dir": owner_home_dir / "trash",
        "owner_capability_requests_dir": owner_home_dir / "capability_requests",
        "owner_temporary_grants_dir": owner_home_dir / "temporary_grants",
        "owner_audit_log_jsonl": owner_home_dir / "audit_log.jsonl",
    }


def _identity_path_fields(home: Path) -> dict[str, Path]:
    identity_dir = home / "identity"
    return {
        "identity_dir": identity_dir,
        "canonical_users_dir": identity_dir / "canonical_users",
        "linked_identities_jsonl": identity_dir / "linked_identities.jsonl",
        "provider_identity_dir": identity_dir / "provider_identity",
    }


def _global_index_path_fields(home: Path) -> dict[str, Path]:
    global_index_dir = home / "global_index"
    return {
        "global_index_dir": global_index_dir,
        "global_index_owners_jsonl": global_index_dir / "owners.jsonl",
        "global_index_active_tasks_jsonl": global_index_dir / "active_tasks.jsonl",
        "global_index_active_runs_jsonl": global_index_dir / "active_runs.jsonl",
        "global_index_active_agents_jsonl": global_index_dir / "active_agents.jsonl",
    }


def _system_path_fields(home: Path) -> dict[str, Path]:
    system_dir = home / "system"
    return {
        "system_dir": system_dir,
        "system_schema_version_json": system_dir / "schema_version.json",
        "system_config_dir": system_dir / "config",
        "system_audit_dir": system_dir / "audit",
        "system_metrics_dir": system_dir / "metrics",
        "system_doctor_dir": system_dir / "doctor",
        "system_backups_dir": system_dir / "backups",
        "system_migrations_dir": system_dir / "migrations",
    }


def _schema_version_payload() -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": HOME_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
        "migration_level": 0,
        "compatible_read_versions": ["my-agent-home.v1", HOME_SCHEMA_VERSION],
        "writer_version": HOME_SCHEMA_VERSION,
    }


def _default_permissions_payload() -> dict[str, object]:
    return {
        "schema_version": "permissions.v1",
        "filesystem": {
            "access_mode": "workspace-write",
            "dangerous_paths": ["/", "/etc", "/System", "~/.ssh"],
        },
        "network": {"enabled": True},
        "shell": {"inherits_parent": True},
        "subagents": {"inheritance": "parent_capped"},
    }


def _default_quota_payload() -> dict[str, object]:
    return {
        "schema_version": "quota.v1",
        "max_active_agents": 1000,
        "max_subagents": 50,
        "max_depth": 4,
        "max_disk_mb": 102400,
    }


def _default_retention_payload() -> dict[str, object]:
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


def _default_skill_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "skill-policy.v1",
        "enabled_sources": ["owner", "workspace", "shared", "builtin"],
        "enabled_shared_skills": [],
        "disabled_skills": [],
        "pin_versions": {},
    }


def _default_tool_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "tool-policy.v1",
        "enabled_sources": ["builtin", "owner", "workspace", "shared"],
        "disabled_tools": [],
        "pin_versions": {},
    }


__all__ = ["v2_home_directories", "v2_home_path_fields", "v2_seed_files", "v2_seed_jsons"]
