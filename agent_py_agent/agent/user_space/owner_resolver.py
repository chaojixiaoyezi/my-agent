# LLM: Owner resolver maps CLI/provider identities to isolated V2 owner homes.
# 模块用途: 解析 local/provider 用户或群的 owner home，并按需初始化 owner 私有入口文件和策略。

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths
from .owner_policy_seed_payloads import (
    default_permissions_payload,
    default_quota_payload,
    default_retention_payload,
    default_skill_policy_payload,
    default_tool_policy_payload,
)


# LLM: OwnerIdentity is the stable identity key before session/task/run lookup.
# 类用途: 描述当前运行属于本地 CLI、外部用户还是外部群空间。
@dataclass(frozen=True)
class OwnerIdentity:
    provider: str
    owner_kind: str
    owner_id: str

    # LLM: local_main is the default CLI/admin owner identity.
    # 函数用途: 返回本地主账号身份，不混入 provider 用户或群空间。
    @classmethod
    def local_main(cls) -> OwnerIdentity:
        return cls(provider="local", owner_kind="main", owner_id="main")

    # LLM: provider_user normalizes external user identities before path resolution.
    # 函数用途: 构造飞书、微信等外部用户 owner 身份，并清洗路径片段。
    @classmethod
    def provider_user(cls, provider: str, owner_id: str) -> OwnerIdentity:
        return cls(provider=_safe_segment(provider), owner_kind="user", owner_id=_safe_segment(owner_id))

    # LLM: provider_group gives group chats their own isolated owner home.
    # 函数用途: 构造外部群空间 owner 身份，让群记忆和个人记忆分开。
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
    memory_hot_md: Path
    permissions_json: Path
    quota_json: Path
    retention_json: Path
    skill_policy_json: Path
    tool_policy_json: Path
    daily_memory_dir: Path
    raw_memory_dir: Path
    hooks_memory_dir: Path
    lessons_memory_dir: Path
    routing_memory_dir: Path
    routing_index_md: Path
    indexes_dir: Path
    long_term_dir: Path
    runtime_refs_dir: Path
    tasks_dir: Path
    runs_dir: Path
    agents_dir: Path
    compact_dir: Path
    workspace_dir: Path
    artifacts_dir: Path
    data_dir: Path
    logs_dir: Path
    cache_dir: Path
    tmp_dir: Path
    trash_dir: Path
    capability_requests_dir: Path
    temporary_grants_dir: Path
    audit_log_jsonl: Path


# LLM: resolve_owner_home is the read-only half of owner path resolution.
# 函数用途: 根据 home 根和 owner 身份计算目录路径，不创建文件。
def resolve_owner_home(root: str | Path, identity: OwnerIdentity | None = None) -> OwnerHomeResult:
    home = Path(root).expanduser().resolve()
    resolved = identity or OwnerIdentity.local_main()
    owner_home = _owner_home_dir(home, resolved)
    return _owner_home_result(home, resolved, owner_home)


# LLM: ensure_owner_home bootstraps a fresh owner home with seed policies.
# 函数用途: 创建 owner 私有目录、入口文档和默认策略 JSON。
def ensure_owner_home(root: str | Path, identity: OwnerIdentity | None = None) -> OwnerHomeResult:
    result = resolve_owner_home(root, identity)
    for directory in _owner_directories(result):
        directory.mkdir(parents=True, exist_ok=True)
    for path, content in _owner_seed_files(result):
        _write_seed_file(path, content)
    for path, payload in _owner_seed_jsons(result):
        _write_seed_json(path, payload)
    return result


# LLM: owner_identity_from_config is the boundary from runtime config to owner identity.
# 函数用途: 从 AgentConfig 读取 local/provider/group 字段并生成稳定 owner 身份。
def owner_identity_from_config(config: Any) -> OwnerIdentity:
    provider = _safe_segment(getattr(config, "my_agent_owner_provider", "local"))
    kind = _safe_segment(getattr(config, "my_agent_owner_kind", "main"))
    owner_id = _safe_segment(getattr(config, "my_agent_owner_id", "main"))
    if provider == "local" and kind == "main":
        return OwnerIdentity.local_main()
    if kind == "group":
        return OwnerIdentity.provider_group(provider, owner_id)
    return OwnerIdentity.provider_user(provider, owner_id)


# LLM: home_paths_with_owner attaches owner-specific paths to the shared home path bundle.
# 函数用途: 把 owner home 的 memory/tasks/agents/policy 路径合并进 MyAgentHomePaths。
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
        owner_memory_hot_md=owner.memory_hot_md,
        owner_permissions_json=owner.permissions_json,
        owner_quota_json=owner.quota_json,
        owner_retention_json=owner.retention_json,
        owner_skill_policy_json=owner.skill_policy_json,
        owner_tool_policy_json=owner.tool_policy_json,
        owner_memory_daily_dir=owner.daily_memory_dir,
        owner_memory_raw_dir=owner.raw_memory_dir,
        owner_memory_hooks_dir=owner.hooks_memory_dir,
        owner_memory_lessons_dir=owner.lessons_memory_dir,
        owner_memory_routing_dir=owner.routing_memory_dir,
        owner_memory_routing_index_md=owner.routing_index_md,
        owner_memory_indexes_dir=owner.indexes_dir,
        owner_memory_long_term_dir=owner.long_term_dir,
        owner_memory_runtime_refs_dir=owner.runtime_refs_dir,
        owner_tasks_dir=owner.tasks_dir,
        owner_runs_dir=owner.runs_dir,
        owner_agents_dir=owner.agents_dir,
        owner_compact_dir=owner.compact_dir,
        owner_workspace_dir=owner.workspace_dir,
        owner_artifacts_dir=owner.artifacts_dir,
        owner_data_dir=owner.data_dir,
        owner_logs_dir=owner.logs_dir,
        owner_cache_dir=owner.cache_dir,
        owner_tmp_dir=owner.tmp_dir,
        owner_trash_dir=owner.trash_dir,
        owner_capability_requests_dir=owner.capability_requests_dir,
        owner_temporary_grants_dir=owner.temporary_grants_dir,
        owner_audit_log_jsonl=owner.audit_log_jsonl,
    )


# LLM: _owner_home_dir is the canonical directory layout for each owner kind.
# 函数用途: 根据 local/user/group 身份返回 owner 私有 home 目录。
def _owner_home_dir(root: Path, identity: OwnerIdentity) -> Path:
    if identity.provider == "local" and identity.owner_kind == "main":
        return root / "owners" / "local" / "main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return root / "owners" / "providers" / identity.provider / bucket / identity.owner_id


# LLM: _owner_home_result builds the full owner path bundle from one home directory.
# 函数用途: 生成 owner 的记忆、任务、compact、策略、缓存和审计路径对象。
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
        memory_hot_md=home_dir / "memory-hot.md",
        permissions_json=home_dir / "permissions.json",
        quota_json=home_dir / "quota.json",
        retention_json=home_dir / "retention.json",
        skill_policy_json=home_dir / "skill_policy.json",
        tool_policy_json=home_dir / "tool_policy.json",
        daily_memory_dir=memory / "daily",
        raw_memory_dir=memory / "raw",
        hooks_memory_dir=memory / "hooks",
        lessons_memory_dir=memory / "lessons",
        routing_memory_dir=memory / "routing",
        routing_index_md=memory / "routing" / "INDEX.md",
        indexes_dir=memory / "indexes",
        long_term_dir=memory / "long_term",
        runtime_refs_dir=memory / "runtime_refs",
        tasks_dir=home_dir / "tasks",
        runs_dir=home_dir / "runs",
        agents_dir=home_dir / "agents",
        compact_dir=home_dir / "compact",
        workspace_dir=home_dir / "workspace",
        artifacts_dir=home_dir / "artifacts",
        data_dir=home_dir / "data",
        logs_dir=home_dir / "logs",
        cache_dir=home_dir / "cache",
        tmp_dir=home_dir / "tmp",
        trash_dir=home_dir / "trash",
        capability_requests_dir=home_dir / "capability_requests",
        temporary_grants_dir=home_dir / "temporary_grants",
        audit_log_jsonl=home_dir / "audit_log.jsonl",
    )


# LLM: _owner_directories lists every directory needed for a fresh owner home.
# 函数用途: 给 ensure_owner_home 创建目录使用，避免目录散落在多处硬编码。
def _owner_directories(result: OwnerHomeResult) -> tuple[Path, ...]:
    return (
        result.home_dir,
        result.daily_memory_dir,
        result.raw_memory_dir,
        result.hooks_memory_dir,
        result.lessons_memory_dir,
        result.routing_memory_dir,
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
        result.data_dir,
        result.logs_dir,
        result.cache_dir,
        result.tmp_dir,
        result.trash_dir,
        result.capability_requests_dir,
        result.temporary_grants_dir,
        result.home_dir / "skills" / ".drafts",
        result.home_dir / "tools" / ".drafts",
    )


# LLM: _owner_seed_files defines human-editable owner entry files.
# 函数用途: 生成 AGENTS/SOUL/USER/memory-hot 等 Markdown 初始内容。
def _owner_seed_files(result: OwnerHomeResult) -> tuple[tuple[Path, str], ...]:
    return (
        (result.soul_md, "# SOUL\n\n"),
        (result.user_md, "# USER\n\n"),
        (result.agents_md, "# AGENTS\n\n"),
        (result.memory_md, "# Memory\n\n"),
        (result.memory_hot_md, "# Memory HOT\n\n"),
        (result.routing_index_md, "# Owner Memory Routing Index\n\n"),
    )


# LLM: _owner_seed_jsons defines machine-readable owner policy files.
# 函数用途: 生成 permissions/quota/retention/skill/tool policy 初始 JSON。
def _owner_seed_jsons(result: OwnerHomeResult) -> tuple[tuple[Path, dict[str, object]], ...]:
    return (
        (result.permissions_json, default_permissions_payload()),
        (result.quota_json, default_quota_payload()),
        (result.retention_json, default_retention_payload()),
        (result.skill_policy_json, default_skill_policy_payload()),
        (result.tool_policy_json, default_tool_policy_payload()),
    )


# LLM: _owner_id is the stable display and index key for an owner.
# 函数用途: 把 provider/kind/id 合成统一 owner_id，供索引和审计引用。
def _owner_id(identity: OwnerIdentity) -> str:
    if identity.provider == "local" and identity.owner_kind == "main":
        return "local/main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return f"providers/{identity.provider}/{bucket}/{identity.owner_id}"


# LLM: _safe_segment protects owner ids before they become path segments.
# 函数用途: 清洗 provider/user/group 标识，避免空值或路径分隔符进入目录名。
def _safe_segment(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in text)
    return result.strip(".-_/") or "unknown"


# LLM: _write_seed_file never overwrites user-edited owner entry files.
# 函数用途: 只在文件不存在时写入 Markdown 种子内容。
def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


# LLM: _write_seed_json preserves existing policy files while bootstrapping new owners.
# 函数用途: 只在 JSON 策略文件不存在时写入默认配置。
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
