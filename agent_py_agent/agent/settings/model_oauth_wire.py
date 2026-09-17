# LLM: 设备码/换令牌只发显式 OAuth 请求；拒绝 HTTP 重定向，不记录原响应或秘密，所有返回值仅供私有存储。
# 模块用途: 实现 ChatGPT 与 RFC 8628 设备码授权的有界网络交互，调用方负责 owner、取消和原子保存。
from __future__ import annotations

import base64
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from ..backends.oauth_transport import NoAuthRedirect
from .model_oauth_schema import CHATGPT_ISSUER, auth_url, verification_url
from .model_provider_schema import ModelProfileError


# LLM: 不沿异常链公开请求体；大小、耗时均有界，只有状态码和协议错误代号参与决策。
# 函数用途: 发一次认证 JSON/form 请求，返回结构化状态；网络错误不泄漏令牌。
def oauth_post(url: str, body: dict, *, as_json: bool = False) -> tuple[int, dict]:
    auth_url(url)
    data = json.dumps(body).encode() if as_json else urlencode(body).encode()
    headers = {"Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded", "Accept": "application/json"}
    try:
        try:
            response = build_opener(NoAuthRedirect).open(Request(url, data=data, headers=headers), timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            status, raw = response.code, response.read(262145)
        if 300 <= status < 400:
            raise ValueError("redirect")
        try:
            obj = json.loads(raw) if len(raw) <= 262144 else None
        except (ValueError, UnicodeError):
            obj = None
        if status >= 400 and not isinstance(obj, dict):
            return status, {}
        if not isinstance(obj, dict):
            raise ValueError("shape")
        return status, obj
    except (URLError, OSError, ValueError):
        raise ModelProfileError("认证服务器未返回可用结果；可重试登录，现有凭据未被清除。") from None


# LLM: 只接受有限的正有效期/轮询间隔；异常值不能形成无限登录或高频轮询。
# 函数用途: 规范服务商返回的设备码时限。
def _seconds(value: object, default: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(maximum, max(1, number)) if math.isfinite(number) else default


# LLM: 登录在显式动作时开始；设备码是短期秘密，不能写聊天或一般日志。
# 函数用途: 获取供用户在服务商网页确认的验证码与等待参数。
def start_device(auth: dict) -> dict:
    body = {"client_id": auth["client_id"]}
    for key in ("scope", "audience", "client_secret"):
        if auth.get(key):
            body[key] = auth[key]
    status, obj = oauth_post(auth["device_url"], body, as_json=auth["mode"] == "chatgpt")
    if status != 200:
        raise ModelProfileError(f"认证服务器拒绝设备码请求（HTTP {status}）；请检查设备码接口与访问限制，尚未进入账号确认。")
    code = obj.get("device_auth_id" if auth["mode"] == "chatgpt" else "device_code")
    user_code = obj.get("user_code", obj.get("usercode"))
    url = CHATGPT_ISSUER + "/codex/device" if auth["mode"] == "chatgpt" else obj.get("verification_uri", obj.get("verification_url"))
    if not all(isinstance(text, str) and text and len(text) <= 8192 and not any(ord(c) < 32 for c in text) for text in (code, user_code)):
        raise ModelProfileError("认证服务器返回的设备码不完整。")
    return {"device_code": code, "user_code": user_code, "verification_uri": verification_url(url),
            "expires_at": time.time() + _seconds(obj.get("expires_in"), 900, 1800),
            "interval": _seconds(obj.get("interval"), 5, 60)}


# LLM: JWT 解码仅用于服务商已返回 token 的期限与账号投递字段，不作为本机身份/权限证明。
# 函数用途: 读取令牌元数据，不保存或显示完整 ID token。
def _claims(token: object) -> dict:
    try:
        part = str(token).split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


# LLM: token 必须为 Bearer；刷新可保留旧 refresh，但不能把其他账号令牌嫁接到原执行快照。
# 函数用途: 从成功响应提取最少凭据及过期时间；不保留邮箱、名字或 ID token。
def token_fields(obj: dict, auth: dict) -> dict:
    access = obj.get("access_token")
    if not isinstance(access, str) or not access or len(access) > 65536 or any(ord(c) < 33 for c in access):
        raise ModelProfileError("认证成功响应缺少有效访问令牌。")
    if str(obj.get("token_type", "Bearer")).lower() != "bearer":
        raise ModelProfileError("当前仅支持 OAuth Bearer 令牌。")
    claims = _claims(access)
    expiry = claims.get("exp")
    expires_at = time.time() + _seconds(obj.get("expires_in"), 3600, 31 * 86400)
    if isinstance(expiry, (int, float)) and math.isfinite(expiry):
        expires_at = float(expiry)
    result = {"access_token": access, "refresh_token": obj.get("refresh_token") or auth.get("refresh_token", ""), "expires_at": expires_at}
    if auth["mode"] == "chatgpt":
        info = _claims(obj.get("id_token")).get("https://api.openai.com/auth", {})
        fallback = claims.get("https://api.openai.com/auth", {})
        info = info if isinstance(info, dict) else {}
        fallback = fallback if isinstance(fallback, dict) else {}
        account = info.get("chatgpt_account_id") or fallback.get("chatgpt_account_id") or auth.get("account_id")
        if not isinstance(account, str) or not account:
            raise ModelProfileError("登录响应缺少订阅账号编号，请重新登录。")
        if auth.get("account_id") and auth["account_id"] != account:
            raise ModelProfileError("刷新结果的账号已变化，请重新登录。")
        result["account_id"] = account
    return result


# LLM: 每次调用只轮询一次；pending/slow_down/expired/denied 按协议区分，不递归等待或自动另开授权。
# 函数用途: 检查用户是否完成网页授权，成功后换取令牌。
def poll_device(auth: dict, pending: dict) -> tuple[str, dict]:
    chatgpt = auth["mode"] == "chatgpt"
    body = ({"device_auth_id": pending["device_code"], "user_code": pending["user_code"]} if chatgpt else
            {"client_id": auth["client_id"], "device_code": pending["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    if not chatgpt and auth.get("client_secret"):
        body["client_secret"] = auth["client_secret"]
    url = CHATGPT_ISSUER + "/api/accounts/deviceauth/token" if chatgpt else auth["token_url"]
    status, obj = oauth_post(url, body, as_json=chatgpt)
    if (chatgpt and status in {403, 404}) or obj.get("error") == "authorization_pending":
        return "pending", {}
    if obj.get("error") == "slow_down":
        return "slow_down", {}
    if status != 200:
        raise ModelProfileError(f"登录未获授权或已过期（HTTP {status}），请重新发起登录。")
    if chatgpt:
        if not obj.get("authorization_code") or not obj.get("code_verifier"):
            raise ModelProfileError("订阅授权响应缺少兑换参数。")
        status, obj = oauth_post(auth["token_url"], {"client_id": auth["client_id"], "grant_type": "authorization_code",
            "code": obj["authorization_code"], "code_verifier": obj["code_verifier"],
            "redirect_uri": CHATGPT_ISSUER + "/deviceauth/callback"})
        if status != 200:
            raise ModelProfileError(f"授权码兑换失败（HTTP {status}），请重新登录。")
    fresh = {key: value for key, value in auth.items() if key not in {"access_token", "refresh_token", "account_id", "expires_at"}}
    return "connected", token_fields(obj, fresh)


# LLM: 刷新只由实际请求触发，失败不回退 API Key、不换模型，也不把 token 放到异常正文。
# 函数用途: 更新到期访问令牌，支持 refresh token 轮换。
def refresh_tokens(auth: dict) -> dict:
    if not auth.get("refresh_token"):
        raise ModelProfileError("登录已过期且没有刷新凭据，请在 /model 重新登录。")
    body = {"client_id": auth["client_id"], "grant_type": "refresh_token", "refresh_token": auth["refresh_token"]}
    if auth.get("client_secret"):
        body["client_secret"] = auth["client_secret"]
    status, obj = oauth_post(auth["token_url"], body, as_json=auth["mode"] == "chatgpt")
    if status != 200:
        raise ModelProfileError(f"登录凭据刷新失败（HTTP {status}），未更换模型；请在 /model 检查登录。")
    return token_fields(obj, auth)
