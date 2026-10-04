
# LLM: G2a 默认保持回环/未知来源旧信任并只观察；G2b 开关（gateway_require_local_credential）打开后回环不再自带信任、
#   来源未知不再放行，可信只由有效本机/配置凭据决定。计数不存用户、请求头或凭据；改判定须同步 test_gateway_local_trust_*。
# 模块用途: 从请求提取可信身份，并观察未迁移的旧本机客户端。
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
import threading
import time
from typing import Any

from .manager import AuthManager
from .models import Action, Permission, Role

logger = logging.getLogger(__name__)

# 回环对端:本机可信来源(适配器/CLI 走 127.0.0.1 调网关)。
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

# LLM: G2b 后旧客户端的可判别拒绝码（设计稿 1.2，9b 建议）：强制档下回环请求没带有效凭据时，
#   它和"远程不可信来源"一样被拒，但两者的处置完全不同——前者只需重启客户端补上凭据，
#   后者是真的越权。只靠 403 分不出来，所以给这一格一个结构化码，客户端据此提示"请重启客户端"，
#   不解析 message。
# 字段用途: 强制档下"回环来源但没带本机凭据"的机器可读错误码。
LOCAL_CREDENTIAL_REQUIRED = "LOCAL_CREDENTIAL_REQUIRED"


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
    """从 HTTP handler 取对端 IP;取不到返回 None,由 _peer_trusted 按当前档位判定(强制档不再当可信)。"""
    addr = getattr(handler, "client_address", None)
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0])
    return None


# LLM: 身份判定与观察分离；迁移档（默认）不改变无凭据回环权限，强制档按开关收紧；秘密只留内存，不输出。
# 类用途: 为Gateway提供身份鉴权和迁移阶段的有界观察。
class AuthMiddleware:
    """Gateway HTTP 请求鉴权中间件。"""

    HEADER_USER_ID = "X-User-Id"
    HEADER_CHANNEL = "X-Channel"
    HEADER_TOKEN = "X-Gateway-Token"

    # LLM: 本机凭据由启动钩子绑定；构造不读文件、不导出环境，观察账仅存进程内。require_local_credential 是 G2b 上线闸：
    #   打开后回环不再自带信任、来源未知不放行，可信只由有效凭据决定；默认 False 保持 G2a 行为（服务端与客户端读同一份配置）。
    # 函数用途: 初始化原鉴权服务和旧客户端观察账。
    def __init__(self, auth_manager: AuthManager, auth_token: str = "",
                 require_local_credential: bool = False) -> None:
        self.auth_manager = auth_manager
        self.auth_token = auth_token  # 暴露部署的局部信任 token(空=只靠回环 peer 信任)
        self._require_local_credential = bool(require_local_credential)
        self._local_client_credential = ""
        self._observation_lock = threading.Lock()
        self._uncredentialed_loopback_by_endpoint: dict[str, dict[str, int | float]] = {}

    # LLM: 仅宿主启动绑定严读凭据，不写盘/日志/env，不替模型提供读取入口。
    # 函数用途: 让鉴权入口识别本机客户端持久凭据。
    def set_local_client_credential(self, credential: str) -> None:
        self._local_client_credential = credential

    # LLM: 冲突头沿原拒绝规则；常量时间比较两种凭据，空配置不接受空头。
    # 函数用途: 检查请求是否已携带本机或配置的Gateway凭据。
    def has_gateway_credential(self, headers: dict[str, str]) -> bool:
        token = _header_value(headers, self.HEADER_TOKEN).encode("utf-8")
        return any(secret and hmac.compare_digest(token, secret.encode("utf-8"))
                   for secret in (self._local_client_credential, self.auth_token))

    # LLM: handler/route来自真实分发，每HTTP请求只记一次；只计credential/admin、真实回环、未凭据，未知peer不计。
    # 函数用途: 记录旧客户端请求，不拦截、不改变身份，不存原路径或请求头。
    def observe_loopback_request(self, handler, route) -> None:
        if route.access_tier not in {"credential", "admin"}:
            return
        peer_ip = _handler_peer_ip(handler)
        if peer_ip is None or not _is_loopback_peer(peer_ip) or self.has_gateway_credential(dict(handler.headers)):
            return
        endpoint = route.template
        with self._observation_lock:
            previous = self._uncredentialed_loopback_by_endpoint.get(endpoint, {})
            self._uncredentialed_loopback_by_endpoint[endpoint] = {"count": int(previous.get("count", 0)) + 1,
                                                                  "last_at": time.time()}

    # LLM: 锁内复制仅模板/count/last_at，不暴露凭据；进程重启从空开始，不是持久累计请求账。
    # 函数用途: 返回公开status可使用的旧客户端计数快照。
    def uncredentialed_loopback_snapshot(self) -> dict[str, dict[str, int | float]]:
        with self._observation_lock:
            return {key: dict(value) for key, value in self._uncredentialed_loopback_by_endpoint.items()}

    # LLM: 强制档（G2b）可信只由有效凭据决定——回环不再自带信任，来源未知（peer_ip=None）不再放行；
    #   迁移档（G2a）保持回环/未知来源旧分支，其它 peer 持两种宿主凭据之一才可信。
    # 函数用途: 判定请求来源在当前档位是否可信。
    def _peer_trusted(self, peer_ip: str | None, headers: dict[str, str]) -> bool:
        """来源是否可信:强制档须携带合法凭据;迁移档回环本机(或来源未知)可信,否则须凭据。"""
        if self._require_local_credential:
            return self.has_gateway_credential(headers)
        if peer_ip is None or _is_loopback_peer(peer_ip):
            return True
        return self.has_gateway_credential(headers)

    # LLM: 只回答"这一格拒绝该不该带 LOCAL_CREDENTIAL_REQUIRED"，不改变任何允许/拒绝结果：
    #   必须同时满足三件事——强制档开着、来源是回环本机、且没有有效凭据。没带凭据和带了错或过期凭据的回环
    #   都算“没有有效凭据”，同样带码（处置都是重启客户端，且两者回的一样，不多出判别信号）；远程不可信来源、
    #   来源未知不带码（远程重启也补不上本机凭据，来源未知证明不了是本机），各按既有形态返回。
    #   判定只看结构化事实（开关档位、对端 IP、凭据比对结果），不解析任何文案。
    # 函数用途: 判断一次来源拒绝是否属于"回环 + 强制档 + 没有有效凭据（没带，或带了错的/过期的）"这一格。
    def needs_local_credential(self, peer_ip: str | None, headers: dict[str, str]) -> bool:
        if not self._require_local_credential:
            return False
        if peer_ip is None or not _is_loopback_peer(peer_ip):
            return False
        return not self.has_gateway_credential(headers)

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
        """来源是否可信(按当前档位:强制档须合法凭据;迁移档回环本机/合法 token)。不可信来源不得提交任务/驱动 agent。"""
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

    G2b（g2bfix1）：强制档下"回环 + 没带凭据"这一格额外带 error_code=LOCAL_CREDENTIAL_REQUIRED，
    让客户端能区分"本机客户端还没换代码/没带凭据"（重启即可）和真的越权来源。状态码与既有
    error/message 保持不变，远程不可信来源不带这个码。
    """
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False
    headers = {key: handler.headers.get(key, "") for key in handler.headers.keys()}
    peer_ip = _handler_peer_ip(handler)
    if mw.check_trusted(headers, peer_ip):
        return False
    body = {"error": "forbidden", "message": "untrusted source:不可信来源不可提交任务"}
    # 只读结构化事实决定要不要加码；不允许从 message 或请求正文反推。
    if mw.needs_local_credential(peer_ip, headers):
        body["error_code"] = LOCAL_CREDENTIAL_REQUIRED
    handler._send_json(403, body)
    return True
