"""LLM: HTTP 请求鉴权中间件 — 从请求中提取身份并进行权限检查。

给人看的解释：
AuthMiddleware 用于 gateway HTTP 服务，对每个请求：
1. 从 HTTP header 提取 X-User-Id 和 X-Channel
2. 如果没有 header → 视为终端（chat），给 ADMIN 权限
3. 调用 authorize 检查操作权限，无权返回 403
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .manager import AuthManager
from .models import Action, Permission, Role

logger = logging.getLogger(__name__)


class AuthMiddleware:
    """Gateway HTTP 请求鉴权中间件。"""

    HEADER_USER_ID = "X-User-Id"
    HEADER_CHANNEL = "X-Channel"

    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth_manager = auth_manager

    def extract_identity(self, headers: dict[str, str]) -> tuple[str, str]:
        """从 HTTP header 提取 user_id 和 channel。

        规则：
        - 有 X-User-Id + X-Channel → 使用 header 值
        - 没有 header → 终端请求，channel=chat，user_id=默认用户

        Args:
            headers: HTTP 请求头

        Returns:
            (user_id, channel)
        """
        user_id = headers.get(self.HEADER_USER_ID, "")
        channel = headers.get(self.HEADER_CHANNEL, "")

        if not user_id and not channel:
            # 终端请求，无任何 header
            return ("admin", "chat")

        if not user_id:
            user_id = "anonymous"

        return (user_id, channel or "unknown")

    def get_permission(self, headers: dict[str, str]) -> Permission:
        """从请求 header 认证并返回权限对象。"""
        user_id, channel = self.extract_identity(headers)
        return self.auth_manager.authenticate(channel, user_id)

    def check_permission(
        self,
        headers: dict[str, str],
        action: Action,
        target_user_id: str | None = None,
    ) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否有权执行指定操作。

        Returns:
            (是否允许, 权限对象, HTTP 状态码, 错误响应体)
        """
        permission = self.get_permission(headers)
        ok = self.auth_manager.authorize(permission, action, target_user_id)
        if not ok:
            return (
                False,
                permission,
                403,
                {
                    "error": "forbidden",
                    "message": f"权限不足：需要 {action.value}，当前角色为 {permission.role.value}",
                },
            )
        return (True, permission, 200, {})

    def require_admin(self, headers: dict[str, str]) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否为管理员。"""
        permission = self.get_permission(headers)
        if permission.role != Role.ADMIN:
            return (
                False,
                permission,
                403,
                {
                    "error": "forbidden",
                    "message": "此操作需要管理员权限",
                },
            )
        return (True, permission, 200, {})

    def require_auth(self, headers: dict[str, str]) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否已认证（任何角色都可以）。"""
        permission = self.get_permission(headers)
        return (True, permission, 200, {})


def extract_user_from_request(handler) -> tuple[str, str]:
    """从 HTTP handler 的 headers 提取 user_id 和 channel。"""
    headers = {}
    for key in handler.headers.keys():
        headers[key] = handler.headers.get(key, "")
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return ("admin", "chat")
    return mw.extract_identity(headers)


def require_permission(handler, action: Action, target_user_id: str | None = None) -> bool:
    """在 handler 方法内调用，完成鉴权并发送响应。

    如果无权，返回 True（已发送响应）；有权返回 False（继续执行）。
    """
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False  # 无中间件，不拦截

    headers = {}
    for key in handler.headers.keys():
        headers[key] = handler.headers.get(key, "")

    ok, permission, status, body = mw.check_permission(headers, action, target_user_id)
    if not ok:
        handler._send_json(status, body)
        return True
    return False


def require_admin_handler(handler) -> bool:
    """在 handler 方法内调用，检查管理员权限。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False

    headers = {}
    for key in handler.headers.keys():
        headers[key] = handler.headers.get(key, "")

    ok, permission, status, body = mw.require_admin(headers)
    if not ok:
        handler._send_json(status, body)
        return True
    return False
