
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .home_layout import MyAgentHomePaths


def home_runtime_status_payload(home: MyAgentHomePaths, *, daily_files: int, task_workspaces: int) -> dict[str, Any]:
    schema = read_json_object_report(home.system_schema_version_json, context="home_runtime_status.schema")
    payload: dict[str, Any] = {
        "root": str(home.root),
        "entry_files": _entry_file_status(home),
        "directories": _directory_status(home),
        "owner_identity": _owner_identity(home),
        "owner": _owner_status(home),
        "identity": _identity_status(home),
        "shared": _shared_status(home),
        "system": _system_status(home),
        "schema": schema.payload,
        "counts": {"daily_files": daily_files, "task_workspaces": task_workspaces},
    }
    if schema.load_error:
        payload["schema_load_error"] = schema.load_error
    return payload


def _entry_file_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "soul_md": _path_status(home.soul_md),
        "user_md": _path_status(home.user_md),
        "agents_md": _path_status(home.agents_md),
        "memory_md": _path_status(home.memory_md),
        "memory_hot_md": _path_status(home.memory_hot_md),
    }


def _owner_identity(home: MyAgentHomePaths) -> dict[str, str]:
    return {
        "provider": str(getattr(home, "owner_provider", "") or "local"),
        "owner_kind": str(getattr(home, "owner_kind", "") or "main"),
        "owner_id": str(getattr(home, "owner_id", "") or "local/main"),
        "owner_home": str(getattr(home, "owner_home_dir", "") or ""),
    }


def _directory_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "workspace_tasks": _path_status(home.workspace_tasks_dir),
        "scripts": _path_status(home.scripts_dir),
        "role_templates": _path_status(home.role_templates_dir),
        "workflows": _path_status(home.workflows_dir),
    }


def _owner_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "home_dir": _path_status(home.owner_home_dir),
        "lifecycle": _owner_lifecycle_status(home),
        "agents_md": _path_status(home.owner_agents_md),
        "memory_md": _path_status(home.owner_memory_md),
        "memory_hot_md": _path_status(home.owner_memory_hot_md),
        "memory_route_index": _path_status(home.owner_memory_routing_index_md),
        "daily_memory": _path_status(home.owner_memory_daily_dir),
        "tasks": _path_status(home.owner_tasks_dir),
        "runs": _path_status(home.owner_runs_dir),
        "agents": _path_status(home.owner_agents_dir),
        "compact": _path_status(home.owner_compact_dir),
        "skill_policy": _path_status(home.owner_skill_policy_json),
        "tool_policy": _path_status(home.owner_tool_policy_json),
    }


def _owner_lifecycle_status(home: MyAgentHomePaths) -> dict[str, Any]:
    path = home.owner_home_dir / "owner_status.json"
    report = read_json_object_report(path, context="home_runtime_status.owner_lifecycle")
    status = "UNKNOWN" if report.load_error else str(report.payload.get("status") or "active")
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "status": status,
        "reason": str(report.payload.get("reason") or ""),
    }
    if report.load_error:
        payload["load_error"] = report.load_error
    return payload


def _shared_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "skills": _path_status(home.shared_skills_dir),
        "tools": _path_status(home.shared_tools_dir),
        "workflows": _path_status(home.shared_workflows_dir),
        "indexes": _path_status(home.shared_indexes_dir),
    }


def _identity_status(home: MyAgentHomePaths) -> dict[str, Any]:
    provider_indexes = [path for path in home.provider_identity_dir.glob("*.jsonl") if path.is_file()] if home.provider_identity_dir.exists() else []
    return {
        "canonical_users": _path_status(home.canonical_users_dir),
        "provider_identity": _path_status(home.provider_identity_dir),
        "provider_index_files": len(provider_indexes),
        "linked_identities": _path_status(home.linked_identities_jsonl),
    }


def _system_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "schema_version": _path_status(home.system_schema_version_json),
        "doctor": _path_status(home.system_doctor_dir),
        "backups": _path_status(home.system_backups_dir),
    }


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
    }


__all__ = ["home_runtime_status_payload"]
