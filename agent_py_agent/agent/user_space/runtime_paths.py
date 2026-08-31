
from __future__ import annotations

"""LLM: Resolve owner-scoped durable paths while separating stable services from project state.

模块用途: 为一个 owner 计算 Gateway、会话、子代理和记忆等运行目录；固定服务不随客户端 cwd 漂移。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimePathResolution:
    """Structured result for current owner-home runtime path selection."""

    paths: dict[str, Path]
    reason: str
    explicit_overrides: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OwnerRuntimePathInputs:
    config: Any
    home: Any
    owner_home: Path
    root: Path
    runtime_root: Path


# LLM: This is the single low-layer owner persistence-root resolver shared by
# Gateway and agent-core. Keep the home_paths preference and legacy agent.root
# fallback stable; callers must not duplicate this selection in higher layers.
# 函数用途: 返回当前代理唯一的 owner 持久化根目录，供会话恢复、归档和运行账本共同使用。
def runtime_owner_root(agent: object) -> Path:
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if isinstance(owner_home, str | Path) and str(owner_home).strip():
        return Path(owner_home)
    return Path(agent.root)


def resolve_runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> RuntimePathResolution:
    if not getattr(home, "owner_home_dir", None):
        raise ValueError("owner home is required for runtime paths")
    paths, overrides = _owner_runtime_paths(config, home, root=root)
    return RuntimePathResolution(
        paths=paths,
        reason="owner_home_runtime",
        explicit_overrides=overrides,
    )


def runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> dict[str, Path]:
    return resolve_runtime_paths_for_agent(config, root, home).paths


def apply_runtime_paths_to_config(config: Any, resolution: RuntimePathResolution) -> None:
    for field_name, path in resolution.paths.items():
        if hasattr(config, field_name):
            setattr(config, field_name, str(path))


# LLM: One resolution combines owner-level services with workspace-hashed state. New durable
# concepts must be placed in exactly one of those scopes and must not be duplicated as fallback.
# 函数用途: 汇总当前 owner 的固定服务目录和当前项目的隔离运行目录。
def _owner_runtime_paths(config: Any, home: Any, *, root: Path) -> tuple[dict[str, Path], tuple[str, ...]]:
    owner_home = Path(home.owner_home_dir)
    owner_workspace = Path(getattr(home, "owner_workspace_dir", owner_home / "workspace"))
    runtime_root = owner_workspace / "runtime" / "workspaces" / _workspace_scope_id(root)
    inputs = _OwnerRuntimePathInputs(config=config, home=home, owner_home=owner_home, root=root, runtime_root=runtime_root)
    paths = {
        **_owner_memory_and_session_paths(inputs),
        **_owner_service_runtime_paths(inputs, owner_workspace=owner_workspace),
        **_owner_workspace_runtime_paths(inputs),
    }
    overrides = tuple(field_name for field_name in paths if str(getattr(config, field_name, "") or ""))
    return paths, overrides


def _owner_memory_and_session_paths(inputs: _OwnerRuntimePathInputs) -> dict[str, Path]:
    config = inputs.config
    home = inputs.home
    owner_home = inputs.owner_home
    root = inputs.root
    memory_path = Path(getattr(home, "owner_memory_long_term_dir", owner_home / "memory" / "long_term")) / "memory.jsonl"
    return {
        "memory_path": memory_path,
        "session_workspace": _configured_or_default(
            config,
            root,
            "session_workspace",
            Path(getattr(home, "owner_sessions_dir", owner_home / "sessions")),
        ),
        "audit_log_path": _configured_or_default(
            config,
            root,
            "audit_log_path",
            Path(getattr(home, "owner_logs_dir", owner_home / "logs")) / "audit",
        ),
        "model_speed_profile_path": _configured_or_default(
            config,
            root,
            "model_speed_profile_path",
            Path(getattr(home, "owner_cache_dir", owner_home / "cache")) / "model_speed_profile.json",
        ),
    }


def _owner_workspace_runtime_paths(inputs: _OwnerRuntimePathInputs) -> dict[str, Path]:
    config = inputs.config
    owner_home = inputs.owner_home
    root = inputs.root
    runtime_root = inputs.runtime_root
    local_store_dir = runtime_root / "local_store"
    return {
        "local_store_path": _configured_or_default(config, root, "local_store_path", local_store_dir / "local.db"),
        "local_store_files_dir": _configured_or_default(config, root, "local_store_files_dir", local_store_dir / "files"),
        "local_store_events_path": _configured_or_default(
            config,
            root,
            "local_store_events_path",
            local_store_dir / "events.jsonl",
        ),
        "subagent_workspace": _configured_or_default(config, root, "subagent_workspace", runtime_root / "subagents"),
        "conversation_workspace": _configured_or_default(
            config,
            root,
            "conversation_workspace",
            runtime_root / "conversations",
        ),
        "collaboration_workspace": _configured_or_default(
            config,
            root,
            "collaboration_workspace",
            runtime_root / "collaboration",
        ),
    }


# LLM: Gateway and adapter paths identify one owner-level service, not one project cwd. Relative
# overrides therefore resolve from the stable owner workspace; request-specific cwd travels in the
# ingress contract and must never select a second daemon queue.
# 函数用途: 为同一用户的所有 TUI、Web 和消息入口返回唯一 Gateway/adapter 运行目录。
def _owner_service_runtime_paths(
    inputs: _OwnerRuntimePathInputs,
    *,
    owner_workspace: Path,
) -> dict[str, Path]:
    service_root = owner_workspace / "runtime" / "services"
    return {
        "gateway_workspace": _configured_owner_service_path(
            inputs.config,
            owner_workspace,
            "gateway_workspace",
            service_root / "gateway",
        ),
        "adapter_workspace": _configured_owner_service_path(
            inputs.config,
            owner_workspace,
            "adapter_workspace",
            service_root / "adapters" / "file",
        ),
    }


# LLM: An explicit absolute service path remains authoritative. Relative values are owner-scoped
# so launching a thin client from another cwd cannot silently derive another service identity.
# 函数用途: 解析 Gateway/adapter 的稳定配置路径；相对值以 owner workspace 为基准。
def _configured_owner_service_path(
    config: Any,
    owner_workspace: Path,
    field_name: str,
    owner_default: Path,
) -> Path:
    raw = str(getattr(config, field_name, "") or "")
    if not raw:
        return owner_default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else owner_workspace / path


def _configured_or_default(config: Any, root: Path, field_name: str, owner_default: Path) -> Path:
    raw = str(getattr(config, field_name, "") or "")
    if not raw:
        return owner_default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(root) / path


def _workspace_scope_id(root: Path) -> str:
    resolved = str(Path(root).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    name = Path(resolved).name or "workspace"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name).strip("-_")
    return f"{safe or 'workspace'}-{digest}"


__all__ = [
    "RuntimePathResolution",
    "apply_runtime_paths_to_config",
    "resolve_runtime_paths_for_agent",
    "runtime_owner_root",
    "runtime_paths_for_agent",
]
