
"""权限管理器 — AuthManager， authenticate 和 authorize 接口。

AuthManager 负责：
1. authenticate(channel, user_id) → 根据通道和用户 ID 认证，返回 Permission
2. authorize(permission, action, target_user_id) → 检查权限是否足够
3. is_admin(user_id) / get_role(user_id) → 快捷查询
"""

from __future__ import annotations

from typing import Any

from .models import Action, Permission, Role, build_permission, infer_role


class AuthManager:
    """权限管理器。"""

    # LLM: 可选配置由宿主显式注入；旧调用仍使用独立值，不猜 owner 或配置路径。
    # 函数用途: 保留兼容构造并声明本进程已有认证流水线。
    def __init__(
        self,
        admin_user_id: str = "admin",
        auth_enabled: bool = True,
        *,
        config: Any | None = None,
    ) -> None:
        from ..settings.live_switches import note_auth_pipeline_present

        self.admin_user_id = admin_user_id
        self.config = config
        self.auth_enabled = auth_enabled
        note_auth_pipeline_present()

    # LLM: 有显式配置时读当前值；setter 仅维护旧接口默认，不反写文件或覆盖注入配置。
    # 函数用途: 为所有认证读口提供实时开关。
    @property
    def auth_enabled(self) -> bool:
        return bool(getattr(self.config, "auth_enabled", self._auth_enabled)) if self.config is not None else self._auth_enabled

    @auth_enabled.setter
    def auth_enabled(self, value: bool) -> None:
        self._auth_enabled = bool(value)

    def authenticate(self, channel: str, user_id: str) -> Permission:
        """根据通道和用户 ID 进行认证，返回权限对象。

        Args:
            channel: 通道名（如 feishu、chat）
            user_id: 用户 ID

        Returns:
            Permission: 对应的权限对象"""
        from ..settings.live_switches import refresh_live_switches

        refresh_live_switches(self.config)
        if not self.auth_enabled:
            # 鉴权关闭时，所有人都是管理员
            return Permission(
                role=Role.ADMIN,
                user_id=user_id,
                channels=[channel] if channel else [],
                can_access_all_users=True,
            )
        return build_permission(channel, user_id, self.admin_user_id)

    def authorize(
        self,
        permission: Permission,
        action: Action,
        target_user_id: str | None = None,
    ) -> bool:
        """检查权限是否足够执行指定操作。

        Args:
            permission: 当前用户的权限
            action: 操作类型
            target_user_id: 目标用户 ID（跨用户操作时需要）

        Returns:
            bool: 是否有权执行"""
        from ..settings.live_switches import refresh_live_switches

        refresh_live_switches(self.config)
        if not self.auth_enabled:
            return True

        if not permission.can(action):
            return False

        # 跨用户数据访问需要 can_access_all_users
        if target_user_id is not None and not permission.can_access_all_users:
            if permission.user_id != target_user_id:
                return False

        return True

    def is_admin(self, user_id: str, channel: str = "chat") -> bool:
        """快捷方法：检查用户是否为管理员。

        Args:
            user_id: 用户 ID
            channel: 通道（影响角色推断）

        Returns:
            bool: 是否是管理员"""
        from ..settings.live_switches import refresh_live_switches

        refresh_live_switches(self.config)
        if not self.auth_enabled:
            return True
        role = infer_role(channel, user_id, self.admin_user_id)
        return role == Role.ADMIN

    def get_role(self, user_id: str, channel: str = "chat") -> Role:
        """快捷方法：获取用户在给定通道下的角色。

        Args:
            user_id: 用户 ID
            channel: 通道

        Returns:
            Role: 角色"""
        from ..settings.live_switches import refresh_live_switches

        refresh_live_switches(self.config)
        if not self.auth_enabled:
            return Role.ADMIN
        return infer_role(channel, user_id, self.admin_user_id)

    def check(
        self,
        channel: str,
        user_id: str,
        action: Action,
        target_user_id: str | None = None,
    ) -> tuple[bool, Permission]:
        """便捷方法：认证 + 鉴权一站完成。

        Args:
            channel: 通道
            user_id: 用户 ID
            action: 操作类型
            target_user_id: 目标用户 ID

        Returns:
            (是否允许, 权限对象)"""
        permission = self.authenticate(channel, user_id)
        ok = self.authorize(permission, action, target_user_id)
        return ok, permission
