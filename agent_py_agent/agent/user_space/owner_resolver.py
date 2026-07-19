
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.path_segments import safe_path_segment
from .home_layout import MyAgentHomePaths
from .owner_policy_seed_payloads import (
    default_permissions_payload,
    default_quota_payload,
    default_retention_payload,
    default_skill_policy_payload,
    default_tool_policy_payload,
)
from .persona_templates import AGENTS_TEMPLATE, SOUL_TEMPLATE, USER_TEMPLATE


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
        return cls(provider=safe_path_segment(provider), owner_kind="user", owner_id=safe_path_segment(owner_id))

    @classmethod
    def provider_group(cls, provider: str, owner_id: str) -> OwnerIdentity:
        return cls(provider=safe_path_segment(provider), owner_kind="group", owner_id=safe_path_segment(owner_id))


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
    memory_hot_md: Path
    permissions_json: Path
    quota_json: Path
    retention_json: Path
    skill_policy_json: Path
    tool_policy_json: Path
    daily_memory_dir: Path
    hooks_memory_dir: Path
    lessons_memory_dir: Path
    routing_memory_dir: Path
    routing_index_md: Path
    indexes_dir: Path
    memory_store_jsonl: Path
    memory_ops_jsonl: Path
    long_term_dir: Path
    runtime_refs_dir: Path
    tasks_dir: Path
    runs_dir: Path
    agents_dir: Path
    compact_dir: Path
    workspace_dir: Path
    artifacts_dir: Path
    audit_dir: Path
    data_dir: Path
    scheduler_dir: Path
    scheduler_store_json: Path
    scheduler_history_jsonl: Path
    logs_dir: Path
    cache_dir: Path
    tmp_dir: Path
    trash_dir: Path
    capability_requests_dir: Path
    temporary_grants_dir: Path
    audit_log_jsonl: Path


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
    provider = safe_path_segment(getattr(config, "my_agent_owner_provider", "local"))
    kind = safe_path_segment(getattr(config, "my_agent_owner_kind", "main"))
    owner_id = safe_path_segment(getattr(config, "my_agent_owner_id", "main"))
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
        # owner_sessions_dir 必须按 scoped owner 重设(OwnerHomeResult 没有 sessions_dir 字段,曾被漏掉→
        # 所有用户的会话落到 base owner 的 sessions,跨用户会话泄露)。按 home_dir/sessions 派生,与 base 布局一致。
        owner_sessions_dir=owner.home_dir / "sessions",
        owner_soul_md=owner.soul_md,
        owner_user_md=owner.user_md,
        owner_agents_md=owner.agents_md,
        owner_memory_md=owner.memory_md,
        owner_memory_hot_md=owner.memory_hot_md,
        owner_memory_dir=owner.home_dir / "memory",
        owner_permissions_json=owner.permissions_json,
        owner_quota_json=owner.quota_json,
        owner_retention_json=owner.retention_json,
        owner_skill_policy_json=owner.skill_policy_json,
        owner_tool_policy_json=owner.tool_policy_json,
        owner_memory_daily_dir=owner.daily_memory_dir,
        owner_memory_hooks_dir=owner.hooks_memory_dir,
        owner_memory_lessons_dir=owner.lessons_memory_dir,
        owner_memory_routing_dir=owner.routing_memory_dir,
        owner_memory_routing_index_md=owner.routing_index_md,
        owner_memory_indexes_dir=owner.indexes_dir,
        owner_memory_store_jsonl=owner.memory_store_jsonl,
        owner_memory_ops_jsonl=owner.memory_ops_jsonl,
        owner_memory_long_term_dir=owner.long_term_dir,
        owner_memory_runtime_refs_dir=owner.runtime_refs_dir,
        owner_tasks_dir=owner.tasks_dir,
        owner_runs_dir=owner.runs_dir,
        owner_agents_dir=owner.agents_dir,
        owner_compact_dir=owner.compact_dir,
        owner_workspace_dir=owner.workspace_dir,
        owner_artifacts_dir=owner.artifacts_dir,
        owner_audit_dir=owner.audit_dir,
        owner_data_dir=owner.data_dir,
        owner_scheduler_dir=owner.scheduler_dir,
        owner_scheduler_store_json=owner.scheduler_store_json,
        owner_scheduler_history_jsonl=owner.scheduler_history_jsonl,
        owner_logs_dir=owner.logs_dir,
        owner_cache_dir=owner.cache_dir,
        owner_tmp_dir=owner.tmp_dir,
        owner_trash_dir=owner.trash_dir,
        owner_capability_requests_dir=owner.capability_requests_dir,
        owner_temporary_grants_dir=owner.temporary_grants_dir,
        owner_audit_log_jsonl=owner.audit_log_jsonl,
    )


def _owner_home_dir(root: Path, identity: OwnerIdentity) -> Path:
    if identity.provider == "local" and identity.owner_kind == "main":
        return root / "owners" / "local" / "main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return root / "owners" / "providers" / identity.provider / bucket / identity.owner_id


def _owner_home_result(root: Path, identity: OwnerIdentity, home_dir: Path) -> OwnerHomeResult:
    memory = home_dir / "memory"
    data = home_dir / "data"
    scheduler = data / "scheduler"
    return OwnerHomeResult(
        root=root,
        identity=identity,
        owner_id=_owner_id(identity),
        home_dir=home_dir,
        agents_md=home_dir / "AGENTS.md",
        soul_md=home_dir / "SOUL.md",
        user_md=home_dir / "USER.md",
        memory_md=home_dir / "memory.md",
        memory_hot_md=home_dir / "memory-hot.md",
        permissions_json=home_dir / "permissions.json",
        quota_json=home_dir / "quota.json",
        retention_json=home_dir / "retention.json",
        skill_policy_json=home_dir / "skill_policy.json",
        tool_policy_json=home_dir / "tool_policy.json",
        daily_memory_dir=memory / "daily",
        hooks_memory_dir=memory / "hooks",
        lessons_memory_dir=memory / "lessons",
        routing_memory_dir=memory / "routing",
        routing_index_md=memory / "routing" / "INDEX.md",
        indexes_dir=memory / "indexes",
        memory_store_jsonl=memory / "store.jsonl",
        memory_ops_jsonl=memory / "ops.jsonl",
        long_term_dir=memory / "long_term",
        runtime_refs_dir=memory / "runtime_refs",
        tasks_dir=home_dir / "tasks",
        runs_dir=home_dir / "runs",
        agents_dir=home_dir / "agents",
        compact_dir=home_dir / "compact",
        workspace_dir=home_dir / "workspace",
        artifacts_dir=home_dir / "artifacts",
        audit_dir=home_dir / "audit",
        data_dir=data,
        scheduler_dir=scheduler,
        scheduler_store_json=scheduler / "store.json",
        scheduler_history_jsonl=scheduler / "history.jsonl",
        logs_dir=home_dir / "logs",
        cache_dir=home_dir / "cache",
        tmp_dir=home_dir / "tmp",
        trash_dir=home_dir / "trash",
        capability_requests_dir=home_dir / "capability_requests",
        temporary_grants_dir=home_dir / "temporary_grants",
        audit_log_jsonl=home_dir / "audit_log.jsonl",
    )


def _owner_directories(result: OwnerHomeResult) -> tuple[Path, ...]:
    return (
        result.home_dir,
        result.daily_memory_dir,
        result.hooks_memory_dir,
        result.lessons_memory_dir,
        result.routing_memory_dir,
        result.indexes_dir,
        result.long_term_dir,
        result.runtime_refs_dir,
        result.tasks_dir,
        result.runs_dir,
        result.agents_dir,
        result.compact_dir / "conversations",
        result.workspace_dir,
        result.artifacts_dir,
        result.audit_dir,
        result.data_dir,
        result.scheduler_dir,
        result.logs_dir,
        result.cache_dir,
        result.tmp_dir,
        result.trash_dir,
        result.capability_requests_dir,
        result.temporary_grants_dir,
        result.home_dir / "skills" / ".drafts",
        result.home_dir / "tools" / ".drafts",
    )


def _owner_seed_files(result: OwnerHomeResult) -> tuple[tuple[Path, str], ...]:
    return (
        (result.soul_md, SOUL_TEMPLATE),
        (result.user_md, USER_TEMPLATE),
        (result.agents_md, AGENTS_TEMPLATE),
        (result.memory_md, "# Memory\n\n"),
        (result.memory_hot_md, "# Memory HOT\n\n"),
        (result.routing_index_md, "# Owner Memory Routing Index\n\n"),
        (result.memory_store_jsonl, ""),
        (result.memory_ops_jsonl, ""),
    )


def _owner_seed_jsons(result: OwnerHomeResult) -> tuple[tuple[Path, dict[str, object]], ...]:
    return (
        (result.permissions_json, default_permissions_payload()),
        (result.quota_json, default_quota_payload()),
        (result.retention_json, default_retention_payload()),
        (result.skill_policy_json, default_skill_policy_payload()),
        (result.tool_policy_json, default_tool_policy_payload()),
    )


def _owner_id(identity: OwnerIdentity) -> str:
    if identity.provider == "local" and identity.owner_kind == "main":
        return "local/main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return f"providers/{identity.provider}/{bucket}/{identity.owner_id}"


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_seed_json(path: Path, payload: dict[str, object]) -> None:
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "OwnerHomeResult",
    "OwnerIdentity",
    "ensure_owner_home",
    "home_paths_with_owner",
    "owner_identity_from_config",
    "resolve_owner_home",
]
