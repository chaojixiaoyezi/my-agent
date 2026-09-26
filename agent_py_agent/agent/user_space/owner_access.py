# LLM: 完整代理与冷宿主管理共用原 owner 权限裁决；这里只接受路径、配置与策略，不初始化 Agent 或持久状态。
# 模块用途: 统一普通 owner 的目录墙和本机管理员显式 Full Access 边界。

from __future__ import annotations

from pathlib import Path


# LLM: 只有结构化 local/main 与显式 Full Access 能解除 owner 墙；主工具组装与冷管理入口必须共用此裁决。
# 函数用途: 统一裁决 owner 硬边界和命令权限档位，供工作区、文件工具、Shell 与 Gateway 共用。
def resolve_owner_scope_and_access(home_paths: object, config: object, owner_policy: object | None = None) -> tuple[str, str]:
    """返回 (owner_scope_root, access_mode)。

    owner home 是普通用户和默认管理员的硬边界。只有 local/main 管理员显式请求
    full-access 才同时解除 owner 墙和 shell 限制；其他 owner 不能仅靠自己的配置
    文本或 owner 目录内文件提权。
    """
    owner_scope_root = str(getattr(home_paths, "owner_home_dir", "") or "")
    requested_access = str(getattr(config, "access_mode", "workspace-write") or "workspace-write")
    requested_access = requested_access.strip().lower().replace("_", "-")
    if requested_access not in {"restricted", "workspace-write", "full-access"}:
        requested_access = "workspace-write"
    if requested_access == "full-access":
        if is_local_admin_owner(home_paths):
            return "", "full-access"
        requested_access = "workspace-write"
    policy_access = str(
        getattr(owner_policy, "shell_access_mode", "") or ""
    ).strip().lower().replace("_", "-")
    requested_access = _narrower_access_mode(requested_access, policy_access)
    return owner_scope_root, requested_access


# LLM: 此函数只判原路径权限中的本机 owner，不代替 HTTP 角色认证；远程用户名为 admin 也不能解除目录墙。
# 函数用途: 判断当前 owner 是否为本机 TUI 的默认管理员身份。
def is_local_admin_owner(home_paths: object) -> bool:
    provider = str(getattr(home_paths, "owner_provider", "") or "").strip().lower()
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip().lower()
    return provider in {"", "local"} and owner_kind in {"", "main"}


# LLM: owner 隔离 Shell 要拒读的宿主根只按结构化身份、全局配置和平台事实裁决，不看命令或自然语言。my-agent 根总是给出
#   （沙箱只在 owner 隔离时用它，Full Access 不生效）；开关 shell_sandbox_hide_user_home 打开、owner 不是本机管理员、且平台
#   沙箱本身不隐藏宿主路径（macOS；Linux bwrap 已不挂载家目录）时，再加用户家目录。配置来自全局配置，owner 无法自行覆盖。
# 函数用途: 算出 owner 隔离的 Shell 沙箱要拒读的宿主目录，供工具装配传给前台、后台和终端三条执行路径。
def owner_hidden_host_roots(home_paths: object, config: object, *, platform_hides_host_paths: bool) -> tuple[str, ...]:
    roots = [str(getattr(home_paths, "root", "") or "")]
    hide_home = bool(getattr(config, "shell_sandbox_hide_user_home", False))
    if hide_home and not platform_hides_host_paths and not is_local_admin_owner(home_paths):
        home = Path.home()
        roots.append(str(home) if home.is_absolute() else "")
    return tuple(dict.fromkeys(root for root in roots if root))


# LLM: owner 策略只缩小非 Full 权限；未知值不构成授权，显式 Full Access 在调用本函数前裁决。
# 函数用途: 从全局请求和 owner 策略中选出更严格的 Shell 权限档位。
def _narrower_access_mode(requested: str, owner_policy: str) -> str:
    rank = {"restricted": 0, "workspace-write": 1, "full-access": 2}
    if owner_policy not in rank:
        return requested
    if requested not in rank:
        return owner_policy
    return min((requested, owner_policy), key=rank.__getitem__)
