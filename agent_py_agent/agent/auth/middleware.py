
# LLM: 可信宿主必须持有 G1 本机或配置凭据；回环和未知 peer 不再授予身份。访问层级仍取原路由表，角色和 owner 在业务入口复核。
# 模块用途: 保留真实重复头并拒绝身份歧义，校验宿主客户端来源、提取渠道身份，并记录无凭据请求的有界计数；不会回落回环管理员。
"""Gateway HTTP 的身份头只在宿主凭据通过后生效，公开查询和独立插件令牌沿原路由合同。"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from typing import Any

from .manager import AuthManager
from .models import Action, Permission, Role

logger = logging.getLogger(__name__)

# 回环地址仅用于缺失凭据诊断和有界计数，不能授予本机可信身份。
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


# LLM: 原HTTPMessage.items保留所有头副本，dict测试载体也支持；None专指冲突，缺头空串不能混成冲突。
# 函数用途: 读取一个不区分大小写的身份字段，拒绝歧义而保留合法缺头宿主终端。
def _header_value(headers, name: str) -> str | None:
    """Read one HTTP header case-insensitively and reject conflicting copies."""
    selected = [
        str(value or "")
        for key, value in headers.items()
        if str(key or "").casefold() == name.casefold()
    ]
    if not selected:
        return ""
    return None if any(value != selected[0] for value in selected[1:]) else selected[0]


def _handler_peer_ip(handler) -> str | None:
    """从 HTTP handler 取对端 IP;取不到返回 None,由 _peer_trusted 校验凭据，未知来源不授予身份。"""
    addr = getattr(handler, "client_address", None)
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0])
    return None


# LLM: 身份判定与有界计数分离；无凭据请求不获得回环例外，秘密只留宿主内存。
# 类用途: 为 Gateway 提供凭据鉴权、可信身份和无凭据请求的有界计数。
class AuthMiddleware:
    """Gateway HTTP 请求鉴权中间件。"""

    HEADER_USER_ID = "X-User-Id"
    HEADER_CHANNEL = "X-Channel"
    HEADER_TOKEN = "X-Gateway-Token"

    # LLM: 构造不读取或导出凭据；临时迁移开关已经退场，所有受管身份头必须经同一凭据校验。
    # 函数用途: 初始化原鉴权服务和旧客户端观察账。
    def __init__(self, auth_manager: AuthManager, auth_token: str = "") -> None:
        self.auth_manager = auth_manager
        self.auth_token = auth_token  # 可选部署凭据，空值不代替 G1 本机凭据。
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
        token = (_header_value(headers, self.HEADER_TOKEN) or "").encode("utf-8")
        return any(secret and hmac.compare_digest(token, secret.encode("utf-8"))
                   for secret in (self._local_client_credential, self.auth_token))

    # LLM: handler/route来自真实分发，每HTTP请求只记一次；只计credential/admin、真实回环、未凭据，未知peer不计。
    # 函数用途: 记录旧客户端请求，不拦截、不改变身份，不存原路径或请求头。
    def observe_loopback_request(self, handler, route) -> None:
        if route.access_tier not in {"credential", "admin"}:
            return
        peer_ip = _handler_peer_ip(handler)
        if peer_ip is None or not _is_loopback_peer(peer_ip) or self.has_gateway_credential(handler.headers):
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

    # LLM: 可信来源同时要求原有效凭据和无歧义身份副本；peer_ip只作诊断，完全缺身份头仍是合法host终端。
    # 函数用途: 检查宿主凭据，阻止模型或其它本机进程借回环地址冒充管理员。
    def _peer_trusted(self, peer_ip: str | None, headers: dict[str, str]) -> bool:
        return (self.has_gateway_credential(headers)
                and all(_header_value(headers, name) is not None for name in (self.HEADER_USER_ID, self.HEADER_CHANNEL)))

    # LLM: 只有明确缺/错误单值凭据的已知回环请求才提示本机凭据；身份或token副本冲突不是缺token。
    # 函数用途: 为现有客户端提供可行动的缺凭据原因，判定不改变身份或授权。
    def needs_local_credential(self, peer_ip: str | None, headers: dict[str, str]) -> bool:
        if (peer_ip is None or not _is_loopback_peer(peer_ip)
                or any(_header_value(headers, name) is None for name in (self.HEADER_TOKEN, self.HEADER_USER_ID, self.HEADER_CHANNEL))):
            return False
        return not self.has_gateway_credential(headers)

    # LLM: 必须区分原头的冲突None与缺失空串；所有handler包装保留HTTPMessage副本，不以dict先抹掉歧义。
    # 函数用途: 在有效宿主来源下读取原用户/通道，拒绝冲突，缺身份的可信本机终端仍是admin。
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
        """来源是否持有有效宿主凭据。不可信来源不得提交任务/驱动 agent。"""
        return self._peer_trusted(peer_ip, headers)


# LLM: 真实header副本不得dict合并，身份只由原凭据/角色门裁决；无middleware保留显式单机合同。
# 函数用途: 从原HTTP请求提取准确身份，不把冲突头当缺头管理员。
def extract_user_from_request(handler) -> tuple[str, str]:
    """从 HTTP handler 的 headers 提取 user_id 和 channel(按对端可信度)。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return ("admin", "chat")
    headers = handler.headers
    return mw.extract_identity(headers, _handler_peer_ip(handler))


# LLM: 保留原HTTPMessage的全部字段副本后查原Action/target，不改业务授权或角色规则。
# 函数用途: 为原handler按真实宿主和用户身份执行权限检查，歧义不进入业务。
def require_permission(handler, action: Action, target_user_id: str | None = None) -> bool:
    """在 handler 方法内调用,完成鉴权并发送响应。无权返回 True(已发响应);有权返回 False。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False  # 无中间件,不拦截(配合 #1:此时只可能是回环本机)
    headers = handler.headers
    ok, _permission, status, body = mw.check_permission(headers, action, target_user_id, _handler_peer_ip(handler))
    if not ok:
        handler._send_json(status, body)
        return True
    return False


# LLM: 管理员判断仍走原认证角色；先保留重复头，冲突不能归为缺身份的合法host终端。
# 函数用途: 检查原handler管理员权限，拒绝错误或歧义来源。
def require_admin_handler(handler) -> bool:
    """在 handler 方法内调用,检查管理员权限。"""
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return False
    headers = handler.headers
    ok, _permission, status, body = mw.require_admin(headers, _handler_peer_ip(handler))
    if not ok:
        handler._send_json(status, body)
        return True
    return False


# LLM: 原受管入站先核凭据和身份头一致性，不提前dict抹副本；冲突403不是缺token提示。
# 函数用途: 拒绝未认证或身份歧义的调用，保留原普通用户合法派工与显式单机合同。
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
    headers = handler.headers
    peer_ip = _handler_peer_ip(handler)
    if mw.check_trusted(headers, peer_ip):
        return False
    body = {"error": "forbidden", "message": "untrusted source:不可信来源不可提交任务"}
    # 只读结构化事实决定要不要加码；不允许从 message 或请求正文反推。
    if mw.needs_local_credential(peer_ip, headers):
        body["error_code"] = LOCAL_CREDENTIAL_REQUIRED
    handler._send_json(403, body)
    return True
