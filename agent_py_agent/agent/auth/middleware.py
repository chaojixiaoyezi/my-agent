
"""HTTP 请求鉴权中间件 — 从请求中提取身份并进行权限检查。

AuthMiddleware 用于 gateway HTTP 服务,对每个请求按"来源可信度"提取身份:
1. 可信来源(回环本机 peer,或携带合法 X-Gateway-Token)→ honor X-User-Id / X-Channel 头(本机适配器/CLI
   转发的真实渠道身份);无头则视为本机终端 = ADMIN。
2. 不可信来源(非回环 peer 且无合法 token)→ 一律匿名 USER,**绝不**认 header 身份、**绝不**给 ADMIN。

为何这样(审计 #2):身份头 X-User-Id/X-Channel 客户端可伪造,旧逻辑"缺头即 admin + 全凭 header 推角色"
让任意网络客户端冒充任意用户/管理员。改为只信任回环本机来源(适配器/CLI 走 127.0.0.1),远程来源降匿名,
配合 #1 的默认 loopback 绑定,杜绝"伪造 X-Channel:chat → admin / 缺头 → admin"。token 供暴露部署做更强校验。
使用 Bearer token 与受限的本地凭据信任。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from .manager import AuthManager
from .models import Action, Permission, Role

logger = logging.getLogger(__name__)

# 回环对端:本机可信来源(适配器/CLI 走 127.0.0.1 调网关)。
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


def _is_loopback_peer(peer_ip: str) -> bool:
    ip = (peer_ip or "").strip().lower().strip("[]")
    return ip in _LOOPBACK_PEERS or ip.startswith("127.")


def _header_value(headers: dict[str, str], name: str) -> str:
    """Read one HTTP header case-insensitively and reject conflicting copies."""
    selected = [
        str(value or "")
        for key, value in headers.items()
        if str(key or "").casefold() == name.casefold()
    ]
    if not selected or any(value != selected[0] for value in selected[1:]):
        return ""
    return selected[0]


def _handler_peer_ip(handler) -> str | None:
    """从 HTTP handler 取对端 IP;取不到返回 None(视为可信,保持既有/单测行为)。"""
    addr = getattr(handler, "client_address", None)
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0])
    return None


class AuthMiddleware:
    """Gateway HTTP 请求鉴权中间件。"""

    HEADER_USER_ID = "X-User-Id"
    HEADER_CHANNEL = "X-Channel"
    HEADER_TOKEN = "X-Gateway-Token"

    def __init__(self, auth_manager: AuthManager, auth_token: str = "") -> None:
        self.auth_manager = auth_manager
        self.auth_token = auth_token  # 暴露部署的局部信任 token(空=只靠回环 peer 信任)

    def _peer_trusted(self, peer_ip: str | None, headers: dict[str, str]) -> bool:
        """来源是否可信:回环本机(或来源未知)可信;否则须携带合法 X-Gateway-Token。"""
        if peer_ip is None or _is_loopback_peer(peer_ip):
            return True
        token = _header_value(headers, self.HEADER_TOKEN)
        return bool(self.auth_token) and hmac.compare_digest(token, self.auth_token)

    def extract_identity(self, headers: dict[str, str], peer_ip: str | None = None) -> tuple[str, str]:
        """提取 (user_id, channel)。不可信来源 → 匿名 USER(不认 header,绝不 admin)。"""
        if not self._peer_trusted(peer_ip, headers):
            # 不可信来源:不认任何 header(连 channel 也不认)——否则伪造 X-Channel:chat/cli 终端通道会经
            # infer_role 骗成 admin。一律固定非终端 channel "external" → 匿名 USER。
            return ("anonymous", "external")
        user_id = _header_value(headers, self.HEADER_USER_ID)
        channel = _header_value(headers, self.HEADER_CHANNEL)
        if not user_id and not channel:
            return ("admin", "chat")  # 可信来源 + 无头 = 本机终端 = admin(单机路径不破)
        if not user_id:
            user_id = "anonymous"
        return (user_id, channel or "unknown")

    def get_permission(self, headers: dict[str, str], peer_ip: str | None = None) -> Permission:
        """从请求 header 认证并返回权限对象。"""
        user_id, channel = self.extract_identity(headers, peer_ip)
        return self.auth_manager.authenticate(channel, user_id)

    def check_permission(
        self,
        headers: dict[str, str],
        action: Action,
        target_user_id: str | None = None,
        peer_ip: str | None = None,
    ) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否有权执行指定操作。返回 (是否允许, 权限对象, HTTP 状态码, 错误响应体)。"""
        permission = self.get_permission(headers, peer_ip)
        ok = self.auth_manager.authorize(permission, action, target_user_id)
        if not ok:
            return (False, permission, 403, {
                "error": "forbidden",
                "message": f"权限不足:需要 {action.value},当前角色为 {permission.role.value}",
            })
        return (True, permission, 200, {})

    def require_admin(self, headers: dict[str, str], peer_ip: str | None = None) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否为管理员。"""
        permission = self.get_permission(headers, peer_ip)
        if permission.role != Role.ADMIN:
            return (False, permission, 403, {"error": "forbidden", "message": "此操作需要管理员权限"})
        return (True, permission, 200, {})

    def require_auth(self, headers: dict[str, str], peer_ip: str | None = None) -> tuple[bool, Permission, int, dict[str, Any]]:
        """检查请求是否已认证(任何角色都可以)。"""
        return (True, self.get_permission(headers, peer_ip), 200, {})

    def check_trusted(self, headers: dict[str, str], peer_ip: str | None = None) -> bool:
        """来源是否可信(回环本机/合法 token)。不可信来源不得提交任务/驱动 agent。"""
        return self._peer_trusted(peer_ip, headers)


def extract_user_from_request(handler) -> tuple[str, str]:
    """从 HTTP handler 的 headers 提取 user_id 和 channel(按对端可信度)。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return ("admin", "chat")
    headers = {key: handler.headers.get(key, "") for key in handler.headers.keys()}
    return mw.extract_identity(headers, _handler_peer_ip(handler))


def require_permission(handler, action: Action, target_user_id: str | None = None) -> bool:
    """在 handler 方法内调用,完成鉴权并发送响应。无权返回 True(已发响应);有权返回 False。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False  # 无中间件,不拦截(配合 #1:此时只可能是回环本机)
    headers = {key: handler.headers.get(key, "") for key in handler.headers.keys()}
    ok, _permission, status, body = mw.check_permission(headers, action, target_user_id, _handler_peer_ip(handler))
    if not ok:
        handler._send_json(status, body)
        return True
    return False


def require_admin_handler(handler) -> bool:
    """在 handler 方法内调用,检查管理员权限。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False
    headers = {key: handler.headers.get(key, "") for key in handler.headers.keys()}
    ok, _permission, status, body = mw.require_admin(headers, _handler_peer_ip(handler))
    if not ok:
        handler._send_json(status, body)
        return True
    return False


def require_trusted_source(handler) -> bool:
    """提交任务端点用:不可信来源(远程且无合法 token)拒绝。无中间件=回环单机,放行。

    与 require_admin 区别:提交自己的任务是普通已认证用户行为(渠道用户应能 ask),只挡不可信来源,
    不按角色挡;停网关/跨用户等才走 require_admin。返回 True=已拒绝(发了 403),False=放行。
    """
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False
    headers = {key: handler.headers.get(key, "") for key in handler.headers.keys()}
    if mw.check_trusted(headers, _handler_peer_ip(handler)):
        return False
    handler._send_json(403, {"error": "forbidden", "message": "untrusted source:不可信来源不可提交任务"})
    return True
