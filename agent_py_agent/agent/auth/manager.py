# LLM: Auth module; keep user/session/token contracts stable for middleware callers.
# 模块用途: 处理认证用户、会话、权限检查和请求中间件。

"""权限管理器 — AuthManager， authenticate 和 authorize 接口。

给人看的解释：
AuthManager 负责：
1. authenticate(channel, user_id) → 根据通道和用户 ID 认证，返回 Permission
2. authorize(permission, action, target_user_id) → 检查权限是否足够
3. is_admin(user_id) / get_role(user_id) → 快捷查询
"""

from __future__ import annotations

from typing import Any

from .models import Action, Permission, Role, build_permission, infer_role


# LLM: AuthManager is a 认证和权限 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 权限管理器。
class AuthManager:
    """权限管理器。"""

    # LLM: AuthManager.__init__ belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 AuthManager 的方法，通常依赖实例字段。
    def __init__(
        self,
        admin_user_id: str = "admin",
        auth_enabled: bool = True,
    ) -> None:
        self.admin_user_id = admin_user_id
        self.auth_enabled = auth_enabled

    # LLM: AuthManager.authenticate belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 根据通道和用户 ID 进行认证，返回权限对象。。
    def authenticate(self, channel: str, user_id: str) -> Permission:
        """根据通道和用户 ID 进行认证，返回权限对象。

        Args:
            channel: 通道名（如 feishu、qq、chat）
            user_id: 用户 ID

        Returns:
            Permission: 对应的权限对象"""
        if not self.auth_enabled:
            # 鉴权关闭时，所有人都是管理员
            return Permission(
                role=Role.ADMIN,
                user_id=user_id,
                channels=[channel] if channel else [],
                can_access_all_users=True,
            )
        return build_permission(channel, user_id, self.admin_user_id)

    # LLM: AuthManager.authorize belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 检查权限是否足够执行指定操作。。
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
        if not self.auth_enabled:
            return True

        if not permission.can(action):
            return False

        # 跨用户数据访问需要 can_access_all_users
        if target_user_id is not None and not permission.can_access_all_users:
            if permission.user_id != target_user_id:
                return False

        return True

    # LLM: AuthManager.is_admin belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 快捷方法：检查用户是否为管理员。。
    def is_admin(self, user_id: str, channel: str = "chat") -> bool:
        """快捷方法：检查用户是否为管理员。

        Args:
            user_id: 用户 ID
            channel: 通道（影响角色推断）

        Returns:
            bool: 是否是管理员"""
        if not self.auth_enabled:
            return True
        role = infer_role(channel, user_id, self.admin_user_id)
        return role == Role.ADMIN

    # LLM: AuthManager.get_role belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 快捷方法：获取用户在给定通道下的角色。。
    def get_role(self, user_id: str, channel: str = "chat") -> Role:
        """快捷方法：获取用户在给定通道下的角色。

        Args:
            user_id: 用户 ID
            channel: 通道

        Returns:
            Role: 角色"""
        if not self.auth_enabled:
            return Role.ADMIN
        return infer_role(channel, user_id, self.admin_user_id)

    # LLM: AuthManager.check belongs to 认证和权限; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 便捷方法：认证 + 鉴权一站完成。。
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
