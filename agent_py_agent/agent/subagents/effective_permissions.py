
# LLM: 子代理权限快照受原 owner 与父级共同封顶，文件/命令/网络不能因换执行片提升。
# 模块用途: 冻结主、子、孙沿用的实际权限上界，不从角色正文产生权限。
from __future__ import annotations

from typing import Any

_ACCESS_MODES = {"restricted", "workspace-write", "full-access"}
_DEFAULT_CHILD_ACCESS_MODE = "workspace-write"


def normalize_access_mode(value: object) -> str:
    mode = str(value or "").strip().lower().replace("_", "-")
    return mode if mode in _ACCESS_MODES else _DEFAULT_CHILD_ACCESS_MODE


def child_shell_access_mode(parent_access_mode: object) -> str:
    mode = normalize_access_mode(parent_access_mode)
    return "restricted" if mode == "restricted" else "workspace-write"


def parent_shell_access_mode(parent_task: Any | None, default: object = "") -> str:
    if parent_task is None:
        return normalize_access_mode(default)
    permissions = getattr(parent_task, "effective_permissions", None)
    if not isinstance(permissions, dict):
        return normalize_access_mode(default)
    shell_mode = str(permissions.get("shell_access_mode") or "").strip()
    if shell_mode:
        return normalize_access_mode(shell_mode)
    return normalize_access_mode(default)


# LLM: 网络总开关沿 owner and 父级权限继承；默认开启保持旧行为，禁用必须传到实际 OS 命令沙箱。
# 函数用途: 为新 child 冻结当前工具、资源与网络上界，后续 runner 不能自行扩大。
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
    network = owner_policy.get("network") if isinstance(owner_policy.get("network"), dict) else {}
    parent_permissions = getattr(parent_task, "effective_permissions", {}) or {}
    return {
        "schema_version": "subagent_effective_permissions.v1",
        "source": "parent_access_mode",
        "owner_id": str(owner_policy.get("owner_id") or ""),
        "owner_home": str(owner_policy.get("owner_home") or ""),
        "parent_access_mode": parent_mode,
        "shell_access_mode": shell_mode,
        "max_shell_access_mode": "workspace-write",
        "shell_can_escalate_without_parent": False,
        "max_subagents": int(quota.get("max_subagents") or 0),
        "max_depth": int(quota.get("max_depth") or 0),
        "disabled_tools": list(tools.get("disabled_tools") or []),
        "network_enabled": bool(network.get("enabled", True) and parent_permissions.get("network_enabled", True)),
    }


__all__ = [
    "child_shell_access_mode",
    "effective_permission_snapshot",
    "normalize_access_mode",
    "parent_shell_access_mode",
]
