# LLM: OAuth 参数与令牌只存 owner 私有 provider；公开投影只含授权类型与状态，不能把令牌放进模型快照。
#   浏览器登录的回调地址只认本机回环上登记过的端口，PKCE 校验码只进私有 pending，改动须同步 test_model_oauth。
# 模块用途: 校验设备码与浏览器登录参数、限定凭据投递端点，并生成不含秘密的运行引用。
from __future__ import annotations

import hashlib
import json
import math
from urllib.parse import urlsplit

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
CHATGPT_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_ISSUER = "https://auth.openai.com"
# 官方授权服务器只接受这两个本机回调端口（与官方命令行登录登记的回调地址一致），换端口会被拒绝。
CHATGPT_CALLBACK_PORTS = (1455, 1457)
CHATGPT_BROWSER_SCOPE = "openid profile email offline_access"
_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


# LLM: 回调地址逐字比对登记值，不接受其它主机、端口、路径或查询；客户端只能从这两个值里选。
# 函数用途: 校验浏览器登录的本机回调地址，给 Gateway 开始登录和读取已保存的等待状态共用。
def chatgpt_callback_uri(value: object) -> str:
    from .model_provider_schema import ModelProfileError

    if value not in {f"http://127.0.0.1:{port}/auth/callback" for port in CHATGPT_CALLBACK_PORTS}:
        raise ModelProfileError("浏览器登录的本机回调地址无效。")
    return str(value)


# LLM: 认证端点必须 TLS 或显式本机回环；禁止 URL 中内嵌秘密、查询和片段，重定向由传输层拒绝。
# 函数用途: 校验登录服务器和凭据目的地；内网非 TLS 地址也不能接收账号令牌。
def auth_url(value: object) -> str:
    from .model_provider_schema import ModelProfileError, validate_base_url

    url = validate_base_url(value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ModelProfileError("OAuth 地址需要 HTTPS；HTTP 仅允许本机回环测试。")
    return url


# LLM: 网页确认地址不接收客户端令牌；允许服务商的查询参数，但仍拒绝凭据、片段和非 TLS 外站。
# 函数用途: 校验要展示给用户的授权页面，避免把合法的带查询参数登录链接当成模型 API 地址。
def verification_url(value: object) -> str:
    from .model_provider_schema import ModelProfileError

    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise ModelProfileError("认证服务器返回的确认地址无效。")
    try:
        parsed = urlsplit(value)
        if parsed.fragment:
            raise ValueError("fragment")
        auth_url(parsed._replace(query="").geturl())
    except ValueError:
        raise ModelProfileError("认证服务器返回的确认地址无效。") from None
    return value


# LLM: 只有显式 mode 选择协议；不按模型名猜订阅类型，不接受表单注入 token、pending 或 generation。
# 函数用途: 将 ChatGPT 登录或通用 OAuth 设备码参数规范成一份配置。
def oauth_config(value: object, api_base: str) -> dict:
    from .model_provider_schema import ModelProfileError

    if not isinstance(value, dict) or value.get("mode") not in {"chatgpt", "oauth_device"}:
        raise ModelProfileError("请选择 ChatGPT 登录或通用 OAuth 设备码授权。")
    mode = value["mode"]
    auth_url(api_base)
    if mode == "chatgpt":
        if api_base != CHATGPT_BASE:
            raise ModelProfileError("ChatGPT 订阅凭据只能发送到内置订阅接口。")
        return {"mode": mode, "client_id": CHATGPT_CLIENT,
                "device_url": CHATGPT_ISSUER + "/api/accounts/deviceauth/usercode",
                "token_url": CHATGPT_ISSUER + "/oauth/token", "scope": ""}
    result = {"mode": mode, "device_url": auth_url(value.get("device_url")),
              "token_url": auth_url(value.get("token_url"))}
    for field in ("client_id", "scope", "audience", "client_secret"):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > 8192 or any(ord(c) < 32 for c in text):
            raise ModelProfileError("OAuth 参数格式不合法。")
        result[field] = text.strip()
    if not result["client_id"]:
        raise ModelProfileError("通用 OAuth 需要服务商签发的 Client ID。")
    return result


# LLM: 读取私有文件时保留宿主写入的状态；秘密不回传客户端，损坏记录失败关闭，不猜成已登录。
# 函数用途: 校验已保存的 OAuth 状态及令牌字段。
def stored_oauth(value: object, api_base: str) -> dict:
    from .model_provider_schema import ModelProfileError

    result = oauth_config(value, api_base)
    for field in ("generation", "account_id", "access_token", "refresh_token"):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > 65536 or any(ord(c) < 32 for c in text):
            raise ModelProfileError("保存的登录凭据无效，请重新登录。")
        if text:
            result[field] = text
    for field in ("expires_at",):
        number = value.get(field, 0)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ModelProfileError("保存的登录有效期无效。")
        result[field] = number
    if "pending" in value:
        result["pending"] = _stored_pending(value["pending"])
    return result


# LLM: 每个服务商只有一个等待中的登录；按 flow 字段分派浏览器或设备码两种形状，只保留各自白名单字段。
# 函数用途: 校验已保存的登录等待状态，损坏时要求重新发起登录。
def _stored_pending(pending: object) -> dict:
    from .model_provider_schema import ModelProfileError

    if not isinstance(pending, dict) or not isinstance(pending.get("id"), str) or not pending["id"]:
        raise ModelProfileError("保存的登录请求无效，请重新发起登录。")
    kept = {"id": pending["id"]}
    if pending.get("flow") == "browser":
        kept.update(_browser_pending(pending))
    elif "device_code" in pending:
        kept.update(_device_pending(pending))
    return kept


# LLM: 设备码与验证码是短期秘密，只校验形状不回显；确认页地址按 verification_url 规则校验。
# 函数用途: 校验设备码登录等待状态里的设备码、验证码、确认页和轮询时间。
def _device_pending(pending: dict) -> dict:
    from .model_provider_schema import ModelProfileError

    kept = {}
    for field in ("device_code", "user_code"):
        text = pending.get(field)
        if not isinstance(text, str) or not text or len(text) > 8192 or any(ord(c) < 32 for c in text):
            raise ModelProfileError("保存的登录请求无效，请重新发起登录。")
        kept[field] = text
    kept["verification_uri"] = verification_url(pending.get("verification_uri"))
    for field in ("expires_at", "interval", "next_poll_at"):
        number = pending.get(field, 0)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ModelProfileError("保存的登录等待参数无效，请重新发起登录。")
        kept[field] = number
    return kept


# LLM: state 与 PKCE 校验码必须是随机生成的 base64url 串（43–128 字符）；损坏记录失败关闭，要求重新登录。
# 函数用途: 校验浏览器登录等待状态里的 state、校验码、回调地址和期限，只保留这几个字段。
def _browser_pending(pending: dict) -> dict:
    from .model_provider_schema import ModelProfileError

    kept = {"flow": "browser", "redirect_uri": chatgpt_callback_uri(pending.get("redirect_uri"))}
    for field in ("state", "code_verifier"):
        text = pending.get(field)
        if not isinstance(text, str) or not 43 <= len(text) <= 128 or not set(text) <= _BASE64URL:
            raise ModelProfileError("保存的浏览器登录请求无效，请重新发起登录。")
        kept[field] = text
    expires_at = pending.get("expires_at", 0)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at) or expires_at < 0:
        raise ModelProfileError("保存的浏览器登录期限无效，请重新发起登录。")
    kept["expires_at"] = expires_at
    return kept


# LLM: 绑定指纹覆盖端点、client 和 scope；编辑后旧工作片不得将原令牌发送到新端点。
# 函数用途: 为请求和异步登录回调提供不可混用的配置身份。
def oauth_binding(provider: dict) -> str:
    config = oauth_config(provider["auth"], provider["api_base"])
    return hashlib.sha256(json.dumps([provider["api_base"], config], sort_keys=True).encode()).hexdigest()


# LLM: 这是可用性投影而非 token 验证；过期但有 refresh 的账号仍可选，由实际请求刷新或明确报错。
# 函数用途: 区分没有密钥的 OAuth 模型与真正未配置模型。
def has_credential(provider: dict) -> bool:
    auth = provider.get("auth", {})
    return bool(auth.get("access_token") and auth.get("generation")) if auth else bool(provider.get("api_key"))
