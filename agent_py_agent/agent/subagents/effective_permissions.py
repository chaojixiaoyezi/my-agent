# LLM: Effective subagent permissions are derived from parent facts, never from child prose.
# 模块用途: 生成子代理最终权限快照；父级 full-access 不会自动下放给子代理。

from __future__ import annotations

from typing import Any

_ACCESS_MODES = {"restricted", "workspace-write", "full-access"}
_DEFAULT_CHILD_ACCESS_MODE = "workspace-write"


# LLM: normalize_access_mode keeps child permission derivation aligned with run_command config names.
# 函数用途: 归一化 access_mode；未知值回退 workspace-write，避免隐式 full-access。
def normalize_access_mode(value: object) -> str:
    mode = str(value or "").strip().lower().replace("_", "-")
    return mode if mode in _ACCESS_MODES else _DEFAULT_CHILD_ACCESS_MODE


# LLM: child_shell_access_mode is the non-escalation rule for subagent shell.
# 函数用途: restricted 继续 restricted；workspace/full 统一下放为 workspace-write。
def child_shell_access_mode(parent_access_mode: object) -> str:
    mode = normalize_access_mode(parent_access_mode)
    return "restricted" if mode == "restricted" else "workspace-write"


# LLM: parent_shell_access_mode reads a parent task's already-effective child-safe shell mode.
# 函数用途: 子代理再派孙代理时，从父代理有效权限继续下推，而不是重新看全局配置。
def parent_shell_access_mode(parent_task: Any | None, fallback: object = "") -> str:
    if parent_task is None:
        return normalize_access_mode(fallback)
    permissions = getattr(parent_task, "effective_permissions", None)
    if not isinstance(permissions, dict):
        return normalize_access_mode(fallback)
    shell_mode = str(permissions.get("shell_access_mode") or "").strip()
    if shell_mode:
        return normalize_access_mode(shell_mode)
    return normalize_access_mode(fallback)


# LLM: effective_permission_snapshot records the child runtime boundary for audit and execution.
# 函数用途: 生成落盘权限快照，供执行上下文、shell 工具和 tree 状态读取。
def effective_permission_snapshot(
    *,
    parent_task: Any | None = None,
    parent_access_mode: object = "",
    owner_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    parent_mode = parent_shell_access_mode(parent_task, parent_access_mode)
    shell_mode = child_shell_access_mode(parent_mode)
    owner_policy = owner_policy if isinstance(owner_policy, dict) else {}
    quota = owner_policy.get("quota") if isinstance(owner_policy.get("quota"), dict) else {}
    tools = owner_policy.get("tools") if isinstance(owner_policy.get("tools"), dict) else {}
    return {
        "schema_version": "subagent_effective_permissions.v1",
        "source": "parent_access_mode",
        # LLM: owner facts are audit scope only; they do not let children mint new permissions.
        "owner_id": str(owner_policy.get("owner_id") or ""),
        "owner_home": str(owner_policy.get("owner_home") or ""),
        "parent_access_mode": parent_mode,
        "shell_access_mode": shell_mode,
        "max_shell_access_mode": "workspace-write",
        "shell_can_escalate_without_parent": False,
        "max_subagents": int(quota.get("max_subagents") or 0),
        "max_depth": int(quota.get("max_depth") or 0),
        "disabled_tools": list(tools.get("disabled_tools") or []),
    }


__all__ = [
    "child_shell_access_mode",
    "effective_permission_snapshot",
    "normalize_access_mode",
    "parent_shell_access_mode",
]
