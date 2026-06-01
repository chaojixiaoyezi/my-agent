# LLM: Home runtime status renders doctor payloads without scanning task or memory bodies.
# 模块用途: 生成 home-status / memory-doctor 的 V2 owner、shared、system 状态块。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths


# LLM: home_runtime_status_payload builds a read-only status object for owner-home debugging.
# 函数用途: 汇总家目录入口文件、owner 身份、共享目录和迁移状态，供 CLI/doctor 展示。
def home_runtime_status_payload(home: MyAgentHomePaths, *, daily_files: int, task_workspaces: int) -> dict[str, Any]:
    return {
        "root": str(home.root),
        "entry_files": _entry_file_status(home),
        "directories": _legacy_directory_status(home),
        "owner_identity": _owner_identity(home),
        "owner": _owner_status(home),
        "identity": _identity_status(home),
        "shared": _shared_status(home),
        "system": _system_status(home),
        "schema": _read_json_object(home.system_schema_version_json),
        "counts": {"daily_files": daily_files, "task_workspaces": task_workspaces},
        "migration": _migration_status(home),
    }


# LLM: _entry_file_status reports top-level human guidance files without reading their bodies.
# 函数用途: 检查根入口文件和记忆索引文件是否存在。
def _entry_file_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "soul_md": _path_status(home.soul_md),
        "user_md": _path_status(home.user_md),
        "agents_md": _path_status(home.agents_md),
        "memory_md": _path_status(home.memory_md),
        "memory_hot_md": _path_status(home.memory_hot_md),
        "memory_route_index": _path_status(home.memory_routing_index_md),
    }


# LLM: _owner_identity exposes the currently resolved owner so multi-user runs are debuggable.
# 函数用途: 返回 provider、owner 类型、owner ID 和 owner home 路径。
def _owner_identity(home: MyAgentHomePaths) -> dict[str, str]:
    return {
        "provider": str(getattr(home, "owner_provider", "") or "local"),
        "owner_kind": str(getattr(home, "owner_kind", "") or "main"),
        "owner_id": str(getattr(home, "owner_id", "") or "local/main"),
        "owner_home": str(getattr(home, "owner_home_dir", "") or ""),
    }


# LLM: _legacy_directory_status keeps old top-level home folders visible during migration.
# 函数用途: 报告旧版 daily/raw/tasks/scripts 等目录状态。
def _legacy_directory_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "memory_daily": _path_status(home.memory_daily_dir),
        "memory_raw": _path_status(home.memory_raw_dir),
        "memory_hooks": _path_status(home.memory_hooks_dir),
        "memory_lessons": _path_status(home.memory_lessons_dir),
        "memory_routing": _path_status(home.memory_routing_dir),
        "memory_indexes": _path_status(home.memory_indexes_dir),
        "workspace_tasks": _path_status(home.workspace_tasks_dir),
        "scripts": _path_status(home.scripts_dir),
        "role_templates": _path_status(home.role_templates_dir),
        "workflows": _path_status(home.workflows_dir),
    }


# LLM: _owner_status reports the authoritative owner-home directories and policy files.
# 函数用途: 检查当前 owner 私有 home、记忆、任务、compact 和能力策略路径。
def _owner_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "home_dir": _path_status(home.owner_home_dir),
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


# LLM: _shared_status reports shared capability shelves separately from owner-private home.
# 函数用途: 检查共享 skills/tools/workflows/indexes 目录。
def _shared_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "skills": _path_status(home.shared_skills_dir),
        "tools": _path_status(home.shared_tools_dir),
        "workflows": _path_status(home.shared_workflows_dir),
        "indexes": _path_status(home.shared_indexes_dir),
    }


# LLM: _identity_status reports identity binding indexes without resolving private memory.
# 函数用途: 检查 canonical user 和 provider identity 索引状态。
def _identity_status(home: MyAgentHomePaths) -> dict[str, Any]:
    provider_indexes = [path for path in home.provider_identity_dir.glob("*.jsonl") if path.is_file()] if home.provider_identity_dir.exists() else []
    return {
        "canonical_users": _path_status(home.canonical_users_dir),
        "provider_identity": _path_status(home.provider_identity_dir),
        "provider_index_files": len(provider_indexes),
        "linked_identities": _path_status(home.linked_identities_jsonl),
    }


# LLM: _system_status reports schema/migration/doctor system folders.
# 函数用途: 检查家目录系统元数据和维护目录。
def _system_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "schema_version": _path_status(home.system_schema_version_json),
        "migrations": _path_status(home.system_migrations_dir),
        "doctor": _path_status(home.system_doctor_dir),
    }


# LLM: _migration_status summarizes legacy data still waiting for owner-home migration.
# 函数用途: 统计旧 daily/raw/task workspace 到当前 owner home 的迁移建议。
def _migration_status(home: MyAgentHomePaths) -> dict[str, dict[str, Any]]:
    return {
        "legacy_daily_memory": _legacy_jsonl_migration_status(
            source=home.memory_daily_dir,
            target=home.owner_memory_daily_dir,
        ),
        "legacy_raw_memory": _legacy_jsonl_migration_status(
            source=home.memory_raw_dir,
            target=home.owner_memory_raw_dir,
        ),
        "legacy_task_workspaces": _legacy_directory_migration_status(
            source=home.workspace_tasks_dir,
            target=home.owner_tasks_dir,
        ),
    }


# LLM: _legacy_jsonl_migration_status counts legacy JSONL data without moving it.
# 函数用途: 统计旧 JSONL 文件数量和记录数量，并给出迁移建议。
def _legacy_jsonl_migration_status(*, source: Path, target: Path) -> dict[str, Any]:
    files = [path for path in source.glob("*.jsonl") if path.is_file()] if source.exists() else []
    return {
        "source": str(source),
        "target": str(target),
        "file_count": len(files),
        "record_count": sum(_jsonl_line_count(path) for path in files),
        "advice": "migrate_legacy_to_owner_home" if files else "",
    }


# LLM: _legacy_directory_migration_status counts legacy task workspaces without scanning contents.
# 函数用途: 统计旧任务工作区数量，并给出迁移建议。
def _legacy_directory_migration_status(*, source: Path, target: Path) -> dict[str, Any]:
    item_count = sum(1 for path in source.rglob("state.json") if path.is_file()) if source.exists() else 0
    return {
        "source": str(source),
        "target": str(target),
        "item_count": item_count,
        "advice": "migrate_legacy_to_owner_home" if item_count else "",
    }


# LLM: _jsonl_line_count is best-effort so doctor output never crashes on unreadable files.
# 函数用途: 统计 JSONL 非空行；读取失败时返回 0。
def _jsonl_line_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


# LLM: _path_status keeps filesystem checks structured and side-effect free.
# 函数用途: 返回路径是否存在、类型和文件大小。
def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
        "size_bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
    }


# LLM: _read_json_object reads optional metadata while tolerating missing or malformed files.
# 函数用途: 读取 JSON 对象；不存在、损坏或非对象时返回空字典。
def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


__all__ = ["home_runtime_status_payload"]
