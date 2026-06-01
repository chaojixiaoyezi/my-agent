# LLM: Owner runtime paths decide where active local ledgers live after V2 home bootstrap.
# 模块用途: 把 SimpleAgent 的 LocalStore、子代理、gateway、conversation、collaboration 路径解析成唯一运行事实源。

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LEGACY_DEFAULTS = {
    "local_store_path": "data/local_store/local.db",
    "local_store_files_dir": "data/local_store/files",
    "local_store_events_path": "data/local_store/events.jsonl",
    "subagent_workspace": "data/subagents",
    "gateway_workspace": "data/gateway",
    "adapter_workspace": "data/adapters/file",
    "session_workspace": "data/sessions",
    "conversation_workspace": "data/conversations",
    "collaboration_workspace": "data/collaboration",
    "notification_store_path": "data/notifications",
    "audit_log_path": "data/audit",
    "model_speed_profile_path": "data/model_speed_profile.json",
}


# LLM: RuntimePathResolution makes owner-home vs legacy fallback visible to startup, tests, and diagnostics.
# 类用途: 保存活跃运行路径、是否回退旧路径、回退原因和显式覆盖字段，避免两套事实源静默混用。
@dataclass(frozen=True)
class RuntimePathResolution:
    """Structured result for active runtime path selection.

    `using_legacy_paths` is intentionally visible so old `data/*` fallback cannot
    quietly become the active source of truth during migration.
    """

    paths: dict[str, Path]
    using_legacy_paths: bool
    reason: str
    explicit_overrides: tuple[str, ...] = ()


# LLM: _OwnerRuntimePathInputs keeps owner runtime helpers below parameter-risk limits.
# 类用途: 打包 owner-home runtime 路径计算所需上下文，避免多个 helper 反复传散装参数。
@dataclass(frozen=True)
class _OwnerRuntimePathInputs:
    config: Any
    home: Any
    owner_home: Path
    root: Path
    runtime_root: Path


# LLM: resolve_runtime_paths_for_agent is the single structured path resolver used by SimpleAgent startup.
# 函数用途: 新安装默认落到 owner home；旧路径回退会明确带原因，不能静默。
def resolve_runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> RuntimePathResolution:
    disabled_reason = _home_runtime_disabled_reason(config, home)
    if disabled_reason:
        return RuntimePathResolution(
            paths=_legacy_runtime_paths(config, root),
            using_legacy_paths=True,
            reason=disabled_reason,
        )
    paths, overrides = _owner_runtime_paths(config, home, root=root)
    return RuntimePathResolution(
        paths=paths,
        using_legacy_paths=False,
        reason="owner_home_runtime",
        explicit_overrides=overrides,
    )


# LLM: runtime_paths_for_agent keeps the old dict-returning API as a compatibility read-through.
# 函数用途: 兼容旧调用方；新代码应读取 resolve_runtime_paths_for_agent 的结构化状态。
def runtime_paths_for_agent(config: Any, root: Path, home: Any | None) -> dict[str, Path]:
    return resolve_runtime_paths_for_agent(config, root, home).paths


# LLM: apply_runtime_paths_to_config lets legacy consumers read the same resolved owner paths.
# 函数用途: 将结构化 runtime path 结果回写到 AgentConfig，避免 session/gateway 等旧入口继续使用 data/* 默认值。
def apply_runtime_paths_to_config(config: Any, resolution: RuntimePathResolution) -> None:
    for field_name, path in resolution.paths.items():
        if hasattr(config, field_name):
            setattr(config, field_name, str(path))


# LLM: legacy_memory_path is read-only compatibility for old local/main memory files.
# 函数用途: 返回旧 memory_path 位置，只用于 fallback 读取，不再作为 owner-home 新写入目标。
def legacy_memory_path(config: Any, root: Path) -> Path:
    user_id = getattr(config, "user_id", "admin") or "admin"
    if user_id != "admin":
        user_data_root = getattr(config, "user_data_root", "data/users") or "data/users"
        from .legacy_user_paths import get_legacy_user_paths

        return get_legacy_user_paths(user_id, Path(root) / user_data_root).memory_path
    return Path(root) / getattr(config, "memory_path", "memory.jsonl")


# LLM: _legacy_runtime_paths keeps bootstrap-disabled mode stable for migration tests.
# 函数用途: 在关闭 home runtime 时按旧配置生成本地运行目录。
def _legacy_runtime_paths(config: Any, root: Path) -> dict[str, Path]:
    user_id = getattr(config, "user_id", "admin") or "admin"
    user_data_root = getattr(config, "user_data_root", "data/users") or "data/users"
    root = Path(root)
    if user_id != "admin":
        from .legacy_user_paths import get_legacy_user_paths

        user_paths = get_legacy_user_paths(user_id, root / user_data_root)
        return {
            "local_store_path": user_paths.local_store_path,
            "local_store_files_dir": user_paths.local_store_files_dir,
            "local_store_events_path": user_paths.local_store_events_path,
            "memory_path": user_paths.memory_path,
            "subagent_workspace": user_paths.subagent_workspace,
            "gateway_workspace": root / config.gateway_workspace,
            "adapter_workspace": root / config.adapter_workspace,
            "session_workspace": root / config.session_workspace,
            "conversation_workspace": root / config.conversation_workspace,
            "collaboration_workspace": root / config.collaboration_workspace,
            "notification_store_path": root / config.notification_store_path,
            "audit_log_path": root / config.audit_log_path,
            "model_speed_profile_path": root / config.model_speed_profile_path,
        }
    return {
        "local_store_path": root / config.local_store_path,
        "local_store_files_dir": root / config.local_store_files_dir,
        "local_store_events_path": root / config.local_store_events_path,
        "memory_path": root / config.memory_path,
        "subagent_workspace": root / config.subagent_workspace,
        "gateway_workspace": root / config.gateway_workspace,
        "adapter_workspace": root / config.adapter_workspace,
        "session_workspace": root / config.session_workspace,
        "conversation_workspace": root / config.conversation_workspace,
        "collaboration_workspace": root / config.collaboration_workspace,
        "notification_store_path": root / config.notification_store_path,
        "audit_log_path": root / config.audit_log_path,
        "model_speed_profile_path": root / config.model_speed_profile_path,
    }


# LLM: _home_runtime_disabled_reason isolates why owner-home runtime cannot be used.
# 函数用途: 返回旧路径回退原因，供日志、诊断和测试直接读取。
def _home_runtime_disabled_reason(config: Any, home: Any | None) -> str | None:
    if not bool(getattr(config, "home_runtime_bootstrap_enabled", True)):
        return "home_runtime_disabled"
    if not getattr(home, "owner_home_dir", None):
        return "owner_home_missing"
    return None


# LLM: _owner_runtime_paths scopes active ledgers by workspace so different repos do not see each other.
# 函数用途: 生成 owner-home 下当前 workspace 的运行账本路径，并尊重非默认显式覆盖。
def _owner_runtime_paths(config: Any, home: Any, *, root: Path) -> tuple[dict[str, Path], tuple[str, ...]]:
    owner_home = Path(home.owner_home_dir)
    owner_workspace = Path(getattr(home, "owner_workspace_dir", owner_home / "workspace"))
    runtime_root = owner_workspace / "runtime" / "workspaces" / _workspace_scope_id(root)
    inputs = _OwnerRuntimePathInputs(config=config, home=home, owner_home=owner_home, root=root, runtime_root=runtime_root)
    paths = {
        **_owner_memory_and_session_paths(inputs),
        **_owner_workspace_runtime_paths(inputs),
    }
    overrides = tuple(
        field_name
        for field_name, legacy_default in _LEGACY_DEFAULTS.items()
        if str(getattr(config, field_name, "") or "") not in {"", legacy_default}
    )
    return paths, overrides


# LLM: _owner_memory_and_session_paths keeps owner-level ledgers out of workspace runtime.
# 函数用途: 返回长期记忆、session 和 owner 日志这类 owner 级路径，避免散落在 data/*。
def _owner_memory_and_session_paths(inputs: _OwnerRuntimePathInputs) -> dict[str, Path]:
    config = inputs.config
    home = inputs.home
    owner_home = inputs.owner_home
    root = inputs.root
    return {
        "memory_path": Path(getattr(home, "owner_memory_long_term_dir", owner_home / "memory" / "long_term")) / "memory.jsonl",
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


# LLM: _owner_workspace_runtime_paths returns checkout-scoped runtime ledgers under owner workspace.
# 函数用途: 返回 LocalStore、gateway、adapter、conversation、collaboration 和默认子代理 locator 路径。
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
        "gateway_workspace": _configured_or_default(config, root, "gateway_workspace", runtime_root / "gateway"),
        "adapter_workspace": _configured_or_default(config, root, "adapter_workspace", runtime_root / "adapters" / "file"),
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
        "notification_store_path": _configured_or_default(
            config,
            root,
            "notification_store_path",
            runtime_root / "notifications",
        ),
    }


# LLM: _configured_or_default prevents legacy data defaults from becoming active facts again.
# 函数用途: 默认 data/* 走 owner home；用户显式改成其他路径时才按配置落盘。
def _configured_or_default(config: Any, root: Path, field_name: str, owner_default: Path) -> Path:
    raw = str(getattr(config, field_name, "") or "")
    if not raw or raw == _LEGACY_DEFAULTS[field_name]:
        return owner_default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(root) / path


# LLM: _workspace_scope_id provides a stable small directory name for per-workspace runtime ledgers.
# 函数用途: 用工作区名称加路径哈希生成 scope，避免不同 checkout 在同一 owner 下串账本。
def _workspace_scope_id(root: Path) -> str:
    resolved = str(Path(root).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    name = Path(resolved).name or "workspace"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name).strip("-_")
    return f"{safe or 'workspace'}-{digest}"


__all__ = [
    "RuntimePathResolution",
    "apply_runtime_paths_to_config",
    "legacy_memory_path",
    "resolve_runtime_paths_for_agent",
    "runtime_paths_for_agent",
]
