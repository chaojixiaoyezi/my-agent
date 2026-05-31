# LLM: Owner resolver maps CLI/provider identities to isolated V2 owner homes.
# 模块用途: 解析 local/provider 用户或群的 owner home，并按需初始化 owner 私有入口文件和策略。

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths


# LLM: OwnerIdentity is the stable identity key before session/task/run lookup.
# 类用途: 描述当前运行属于本地 CLI、外部用户还是外部群空间。
@dataclass(frozen=True)
class OwnerIdentity:
    provider: str
    owner_kind: str
    owner_id: str

    @classmethod
    def local_main(cls) -> OwnerIdentity:
        return cls(provider="local", owner_kind="main", owner_id="main")

    @classmethod
    def provider_user(cls, provider: str, owner_id: str) -> OwnerIdentity:
        return cls(provider=_safe_segment(provider), owner_kind="user", owner_id=_safe_segment(owner_id))

    @classmethod
    def provider_group(cls, provider: str, owner_id: str) -> OwnerIdentity:
        return cls(provider=_safe_segment(provider), owner_kind="group", owner_id=_safe_segment(owner_id))


# LLM: OwnerHomeResult is the path bundle handed to runtime, provider adapters, and future migrations.
# 类用途: 保存某个 owner 的私有 home、记忆、任务、能力和策略文件路径。
@dataclass(frozen=True)
class OwnerHomeResult:
    root: Path
    identity: OwnerIdentity
    owner_id: str
    home_dir: Path
    agents_md: Path
    soul_md: Path
    user_md: Path
    memory_md: Path
    permissions_json: Path
    quota_json: Path
    retention_json: Path
    skill_policy_json: Path
    tool_policy_json: Path
    daily_memory_dir: Path
    raw_memory_dir: Path
    hooks_memory_dir: Path
    indexes_dir: Path
    long_term_dir: Path
    runtime_refs_dir: Path
    tasks_dir: Path
    runs_dir: Path
    agents_dir: Path
    compact_dir: Path
    workspace_dir: Path
    artifacts_dir: Path
    capability_requests_dir: Path
    temporary_grants_dir: Path


def resolve_owner_home(root: str | Path, identity: OwnerIdentity | None = None) -> OwnerHomeResult:
    home = Path(root).expanduser().resolve()
    resolved = identity or OwnerIdentity.local_main()
    owner_home = _owner_home_dir(home, resolved)
    return _owner_home_result(home, resolved, owner_home)


def ensure_owner_home(root: str | Path, identity: OwnerIdentity | None = None) -> OwnerHomeResult:
    result = resolve_owner_home(root, identity)
    for directory in _owner_directories(result):
        directory.mkdir(parents=True, exist_ok=True)
    for path, content in _owner_seed_files(result):
        _write_seed_file(path, content)
    for path, payload in _owner_seed_jsons(result):
        _write_seed_json(path, payload)
    return result


def owner_identity_from_config(config: Any) -> OwnerIdentity:
    provider = _safe_segment(getattr(config, "my_agent_owner_provider", "local"))
    kind = _safe_segment(getattr(config, "my_agent_owner_kind", "main"))
    owner_id = _safe_segment(getattr(config, "my_agent_owner_id", "main"))
    if provider == "local" and kind == "main":
        return OwnerIdentity.local_main()
    if kind == "group":
        return OwnerIdentity.provider_group(provider, owner_id)
    return OwnerIdentity.provider_user(provider, owner_id)


def home_paths_with_owner(paths: MyAgentHomePaths, owner: OwnerHomeResult) -> MyAgentHomePaths:
    return replace(
        paths,
        owner_provider=owner.identity.provider,
        owner_kind=owner.identity.owner_kind,
        owner_id=owner.owner_id,
        owner_home_dir=owner.home_dir,
        owner_soul_md=owner.soul_md,
        owner_user_md=owner.user_md,
        owner_agents_md=owner.agents_md,
        owner_memory_md=owner.memory_md,
        owner_permissions_json=owner.permissions_json,
        owner_quota_json=owner.quota_json,
        owner_retention_json=owner.retention_json,
        owner_skill_policy_json=owner.skill_policy_json,
        owner_tool_policy_json=owner.tool_policy_json,
        owner_memory_daily_dir=owner.daily_memory_dir,
        owner_memory_raw_dir=owner.raw_memory_dir,
        owner_memory_hooks_dir=owner.hooks_memory_dir,
        owner_memory_indexes_dir=owner.indexes_dir,
        owner_memory_long_term_dir=owner.long_term_dir,
        owner_memory_runtime_refs_dir=owner.runtime_refs_dir,
        owner_tasks_dir=owner.tasks_dir,
        owner_runs_dir=owner.runs_dir,
        owner_agents_dir=owner.agents_dir,
        owner_compact_dir=owner.compact_dir,
        owner_workspace_dir=owner.workspace_dir,
        owner_artifacts_dir=owner.artifacts_dir,
        owner_capability_requests_dir=owner.capability_requests_dir,
        owner_temporary_grants_dir=owner.temporary_grants_dir,
    )


def _owner_home_dir(root: Path, identity: OwnerIdentity) -> Path:
    if identity.provider == "local" and identity.owner_kind == "main":
        return root / "owners" / "local" / "main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return root / "owners" / "providers" / identity.provider / bucket / identity.owner_id


def _owner_home_result(root: Path, identity: OwnerIdentity, home_dir: Path) -> OwnerHomeResult:
    memory = home_dir / "memory"
    return OwnerHomeResult(
        root=root,
        identity=identity,
        owner_id=_owner_id(identity),
        home_dir=home_dir,
        agents_md=home_dir / "AGENTS.md",
        soul_md=home_dir / "SOUL.md",
        user_md=home_dir / "USER.md",
        memory_md=home_dir / "memory.md",
        permissions_json=home_dir / "permissions.json",
        quota_json=home_dir / "quota.json",
        retention_json=home_dir / "retention.json",
        skill_policy_json=home_dir / "skill_policy.json",
        tool_policy_json=home_dir / "tool_policy.json",
        daily_memory_dir=memory / "daily",
        raw_memory_dir=memory / "raw",
        hooks_memory_dir=memory / "hooks",
        indexes_dir=memory / "indexes",
        long_term_dir=memory / "long_term",
        runtime_refs_dir=memory / "runtime_refs",
        tasks_dir=home_dir / "tasks",
        runs_dir=home_dir / "runs",
        agents_dir=home_dir / "agents",
        compact_dir=home_dir / "compact",
        workspace_dir=home_dir / "workspace",
        artifacts_dir=home_dir / "artifacts",
        capability_requests_dir=home_dir / "capability_requests",
        temporary_grants_dir=home_dir / "temporary_grants",
    )


def _owner_directories(result: OwnerHomeResult) -> tuple[Path, ...]:
    return (
        result.home_dir,
        result.daily_memory_dir,
        result.raw_memory_dir,
        result.hooks_memory_dir,
        result.indexes_dir,
        result.long_term_dir,
        result.runtime_refs_dir,
        result.tasks_dir,
        result.runs_dir,
        result.agents_dir,
        result.compact_dir / "by_task",
        result.compact_dir / "by_run",
        result.compact_dir / "by_agent",
        result.workspace_dir,
        result.artifacts_dir,
        result.capability_requests_dir,
        result.temporary_grants_dir,
        result.home_dir / "skills" / ".drafts",
        result.home_dir / "tools" / ".drafts",
    )


def _owner_seed_files(result: OwnerHomeResult) -> tuple[tuple[Path, str], ...]:
    return (
        (result.soul_md, "# SOUL\n\n"),
        (result.user_md, "# USER\n\n"),
        (result.agents_md, "# AGENTS\n\n"),
        (result.memory_md, "# Memory\n\n"),
    )


def _owner_seed_jsons(result: OwnerHomeResult) -> tuple[tuple[Path, dict[str, object]], ...]:
    return (
        (result.permissions_json, _default_permissions_payload()),
        (result.quota_json, _default_quota_payload()),
        (result.retention_json, _default_retention_payload()),
        (result.skill_policy_json, _default_skill_policy_payload()),
        (result.tool_policy_json, _default_tool_policy_payload()),
    )


def _owner_id(identity: OwnerIdentity) -> str:
    if identity.provider == "local" and identity.owner_kind == "main":
        return "local/main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return f"providers/{identity.provider}/{bucket}/{identity.owner_id}"


def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in text)
    return result.strip(".-_/") or "unknown"


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_seed_json(path: Path, payload: dict[str, object]) -> None:
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_permissions_payload() -> dict[str, object]:
    return {
        "schema_version": "permissions.v1",
        "filesystem": {"access_mode": "workspace-write", "dangerous_paths": ["/", "/etc", "/System", "~/.ssh"]},
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


__all__ = [
    "OwnerHomeResult",
    "OwnerIdentity",
    "ensure_owner_home",
    "home_paths_with_owner",
    "owner_identity_from_config",
    "resolve_owner_home",
]
