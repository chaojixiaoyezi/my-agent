
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .home_layout_v2 import v2_home_directories, v2_home_path_fields, v2_seed_files, v2_seed_jsons
from .home_memory_seeds import (
    default_memory_hot_md,
    default_memory_lessons,
    default_memory_md,
    default_memory_route_index_md,
)

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"


@dataclass(frozen=True)
class MyAgentHomePaths:
    root: Path
    soul_md: Path
    user_md: Path
    agents_md: Path
    memory_md: Path
    memory_hot_md: Path
    config_dir: Path
    scripts_dir: Path
    workspace_dir: Path
    workspace_tasks_dir: Path
    data_dir: Path
    providers_dir: Path
    memory_archive_dir: Path
    skills_dir: Path
    tools_dir: Path
    role_templates_dir: Path
    workflows_dir: Path
    logs_dir: Path
    cache_dir: Path
    tmp_dir: Path
    shared_dir: Path
    shared_builtin_dir: Path
    shared_tools_dir: Path
    shared_skills_dir: Path
    shared_optional_skills_dir: Path
    shared_workflows_dir: Path
    shared_role_templates_dir: Path
    shared_policy_templates_dir: Path
    shared_scripts_dir: Path
    shared_indexes_dir: Path
    shared_indexes_tools_jsonl: Path
    shared_indexes_skills_jsonl: Path
    shared_indexes_workflows_jsonl: Path
    shared_indexes_role_templates_jsonl: Path
    owners_dir: Path
    local_owners_dir: Path
    owner_home_dir: Path
    owner_soul_md: Path
    owner_user_md: Path
    owner_agents_md: Path
    owner_memory_md: Path
    owner_memory_hot_md: Path
    owner_permissions_json: Path
    owner_quota_json: Path
    owner_retention_json: Path
    owner_skill_policy_json: Path
    owner_tool_policy_json: Path
    owner_sessions_dir: Path
    owner_memory_dir: Path
    owner_memory_daily_dir: Path
    owner_memory_hooks_dir: Path
    owner_memory_lessons_dir: Path
    owner_memory_routing_dir: Path
    owner_memory_routing_index_md: Path
    owner_memory_indexes_dir: Path
    owner_memory_store_jsonl: Path
    owner_memory_ops_jsonl: Path
    owner_memory_long_term_dir: Path
    owner_memory_runtime_refs_dir: Path
    owner_tasks_dir: Path
    owner_runs_dir: Path
    owner_agents_dir: Path
    owner_compact_dir: Path
    owner_workspace_dir: Path
    owner_artifacts_dir: Path
    owner_audit_dir: Path
    owner_data_dir: Path
    owner_logs_dir: Path
    owner_cache_dir: Path
    owner_tmp_dir: Path
    owner_trash_dir: Path
    owner_capability_requests_dir: Path
    owner_temporary_grants_dir: Path
    owner_audit_log_jsonl: Path
    identity_dir: Path
    canonical_users_dir: Path
    linked_identities_jsonl: Path
    provider_identity_dir: Path
    global_index_dir: Path
    global_index_owners_jsonl: Path
    global_index_active_tasks_jsonl: Path
    global_index_active_runs_jsonl: Path
    global_index_active_agents_jsonl: Path
    system_dir: Path
    system_schema_version_json: Path
    system_config_dir: Path
    system_audit_dir: Path
    system_metrics_dir: Path
    system_doctor_dir: Path
    system_backups_dir: Path
    owner_provider: str = ""
    owner_kind: str = ""
    owner_id: str = ""


@dataclass(frozen=True)
class RouteIndexTarget:
    path: Path
    authority_root: Path


def resolve_my_agent_home(value: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    env_value = source.get("MY_AGENT_HOME")
    raw = value if value is not None else env_value if env_value else "~/.my-agent"
    return Path(raw).expanduser().resolve()


def home_paths(root: str | Path | None = None) -> MyAgentHomePaths:
    home = resolve_my_agent_home(root)
    return MyAgentHomePaths(
        root=home,
        **_root_home_path_fields(home),
        **v2_home_path_fields(home),
    )


def _root_home_path_fields(home: Path) -> dict[str, Path]:
    workspace_dir = home / "workspace"
    return {
        "soul_md": home / "SOUL.md",
        "user_md": home / "USER.md",
        "agents_md": home / "AGENTS.md",
        "memory_md": home / "memory.md",
        "memory_hot_md": home / "memory-hot.md",
        "config_dir": home / "config",
        "scripts_dir": home / "scripts",
        "workspace_dir": workspace_dir,
        "workspace_tasks_dir": workspace_dir / "tasks",
        "data_dir": home / "data",
        "providers_dir": home / "providers",
        "memory_archive_dir": home / "memory_archive",
        "skills_dir": home / "skills",
        "tools_dir": home / "tools",
        "role_templates_dir": home / "role_templates",
        "workflows_dir": home / "workflows",
        "logs_dir": home / "logs",
        "cache_dir": home / "cache",
        "tmp_dir": home / "tmp",
    }


def ensure_my_agent_home(root: str | Path | None = None) -> MyAgentHomePaths:
    paths = home_paths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    for directory in _HOME_DIRECTORIES(paths):
        directory.mkdir(parents=True, exist_ok=True)
    _write_seed_file(paths.soul_md, "# SOUL\n\n")
    _write_seed_file(paths.user_md, "# USER\n\n")
    _write_seed_file(paths.agents_md, "# AGENTS\n\n")
    _write_seed_file(paths.memory_md, default_memory_md())
    _write_seed_file(paths.memory_hot_md, default_memory_hot_md())
    _write_seed_file(paths.owner_memory_routing_index_md, default_memory_route_index_md())
    for lesson_name, lesson_content in default_memory_lessons().items():
        _write_seed_file(paths.owner_memory_lessons_dir / lesson_name, lesson_content)
    for path, content in v2_seed_files(paths):
        _write_seed_file(path, content)
    for path, payload in v2_seed_jsons(paths):
        _write_seed_json(path, payload)
    return paths


def _HOME_DIRECTORIES(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    return (
        paths.config_dir,
        paths.scripts_dir,
        paths.data_dir,
        paths.providers_dir,
        paths.memory_archive_dir / "artifacts",
        paths.memory_archive_dir / "compact_applies",
        paths.memory_archive_dir / "snapshots",
        paths.memory_archive_dir / "tokens",
        paths.skills_dir,
        paths.tools_dir,
        paths.role_templates_dir,
        paths.workflows_dir,
        paths.logs_dir,
        paths.cache_dir,
        paths.tmp_dir,
        *v2_home_directories(paths),
    )


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_seed_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_task_slug(task_name: str, *, max_chars: int = 80) -> str:
    text = str(task_name or "task").strip().lower()
    slug = _collapse_dashes("".join(_slug_char(char) for char in text)).strip("-_")
    return _trim_slug(slug, max_chars=max_chars)


def _slug_char(char: str) -> str:
    return char if char.isalnum() or char in {"_", "-"} else "-"


def _collapse_dashes(text: str) -> str:
    while "--" in text:
        text = text.replace("--", "-")
    return text


def _trim_slug(slug: str, *, max_chars: int) -> str:
    if not slug:
        return "task"
    return slug[:max_chars].strip("-_") or "task"


def task_workspace_path(
    home: str | Path,
    template: str,
    *,
    date: str,
    task_name: str,
) -> Path:
    task_slug = safe_task_slug(task_name)
    rendered = str(template or "tasks/{date}/{task_slug}").format(
        date=date,
        task_slug=task_slug,
        task_name=task_slug,
    )
    path = Path(rendered)
    if path.is_absolute():
        return path
    return Path(home) / path


def resolve_route_index_target(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> RouteIndexTarget:
    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return RouteIndexTarget(path=candidate.resolve(), authority_root=root)
    workspace_index = (root / candidate).resolve()
    if raw_index or workspace_index.exists() or home_paths is None:
        return RouteIndexTarget(path=workspace_index, authority_root=root)
    home_index = getattr(home_paths, "owner_memory_routing_index_md", None)
    home_root = getattr(home_paths, "owner_home_dir", None)
    if home_index is not None and home_root is not None:
        return RouteIndexTarget(path=Path(home_index).resolve(), authority_root=Path(home_root).resolve())
    return RouteIndexTarget(path=workspace_index, authority_root=root)


def runtime_route_root_and_index(agent) -> tuple[Path, str]:
    target = resolve_route_index_target(agent.root, None, home_paths=getattr(agent, "home_paths", None))
    if target.authority_root == agent.root:
        return target.authority_root, DEFAULT_ROUTE_INDEX.as_posix()
    return target.authority_root, DEFAULT_ROUTE_INDEX.as_posix()
