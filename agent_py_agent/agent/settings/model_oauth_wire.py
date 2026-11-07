# LLM: 设备码/浏览器登录/换令牌只发显式 OAuth 请求；拒绝 HTTP 重定向，不记录原响应或秘密，所有返回值仅供私有存储。
# 模块用途: 实现 ChatGPT 设备码与浏览器（PKCE）登录、RFC 8628 设备码授权的有界网络交互，调用方负责 owner、取消和原子保存。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from ..backends.oauth_transport import NoAuthRedirect
from .model_oauth_schema import (
    CHATGPT_BROWSER_SCOPE,
    CHATGPT_ISSUER,
    SIWC_BASE,
    SIWC_CLIENT,
    SIWC_SCOPE,
    auth_url,
    is_siwc,
    verification_url,
)
from .model_provider_schema import ModelProfileError

# 浏览器登录的等待期限：超过就要求重新点登录，不让一次授权无限期有效。
BROWSER_LOGIN_SECONDS = 600


# LLM: 不沿异常链公开请求体；大小、耗时均有界，只有状态码和协议错误代号参与决策。
# 函数用途: 发一次认证 JSON/form 请求，返回结构化状态；网络错误不泄漏令牌。
def oauth_post(url: str, body: dict, *, as_json: bool = False, allow_empty: bool = False) -> tuple[int, dict]:
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
        if allow_empty and status == 200 and not raw:
            return status, {}
        try:
            obj = json.loads(raw) if len(raw) <= 262144 else None
        except (ValueError, UnicodeError):
            obj = None
        if status >= 400 and not isinstance(obj, dict):
            return status, {}
        if not isinstance(obj, dict):
            raise ValueError("shape")
        return status, obj
    except (HTTPException, URLError, OSError, ValueError):
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
# 函数用途: 提取凭据与期限；SIWC额外保留验签后的身份及ID令牌，供同账号再授权使用。
def token_fields(obj: dict, auth: dict, *, nonce: str | None = None, require_id: bool = False) -> dict:
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
    if is_siwc(auth):
        if obj.get("id_token") or require_id:
            identity = _siwc_identity(obj.get("id_token"), auth, nonce=nonce)
            result.update(id_token=obj["id_token"], issuer=identity["iss"], subject=identity["sub"],
                          email=identity.get("email", ""))
        granted = obj.get("scope")
        if granted is not None and not isinstance(granted, str):
            raise ModelProfileError("认证服务器返回的授权范围格式无效。")
        result["scopes"] = list(dict.fromkeys(granted.split())) if granted is not None else list(auth.get("scopes", []))
        result["client_id"] = auth["client_id"]
        return result
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
    return "connected", token_fields(obj, _without_credentials(auth))


# LLM: 新登录不继承旧令牌、范围或旧Codex账号编号；SIWC保留已验证subject，防止返回账号被替换。
# 函数用途: 去掉已保存的凭据字段，只留认证参数，供新登录兑换令牌时使用。
def _without_credentials(auth: dict) -> dict:
    return {key: value for key, value in auth.items() if key not in {"access_token", "refresh_token", "account_id", "expires_at", "id_token", "scopes"}}


# LLM: 与官方命令行登录同一套授权码 + PKCE(S256) 流程；校验码只返回给调用方写进私有 pending，授权地址只含挑战值与 state，
#   不带官方命令行的来源标识；不联网、不打开浏览器。
# 函数用途: 生成一次浏览器登录要打开的官方授权页地址，以及要私下保存的等待状态。
def browser_authorize(auth: dict, redirect_uri: str) -> tuple[str, dict]:
    verifier = _base64url(secrets.token_bytes(64))
    state = _base64url(secrets.token_bytes(32))
    params = {
        "response_type": "code", "client_id": auth["client_id"], "redirect_uri": redirect_uri,
        "scope": CHATGPT_BROWSER_SCOPE, "code_challenge": _base64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256", "state": state,
        "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true",
    }
    pending = {"flow": "browser", "state": state, "code_verifier": verifier, "redirect_uri": redirect_uri,
               "expires_at": time.time() + BROWSER_LOGIN_SECONDS}
    if is_siwc(auth):
        params.pop("id_token_add_organizations")
        params.pop("codex_cli_simplified_flow")
        nonce = _base64url(secrets.token_bytes(32))
        params.update(scope=SIWC_SCOPE, resource=SIWC_BASE, ext_agent_host_id=auth["ext_agent_host_id"], nonce=nonce)
        pending.update(nonce=nonce, resource=SIWC_BASE, client_id=auth["client_id"])
        if auth["client_id"] == SIWC_CLIENT:
            params["agent_name_hint"] = "my-agent"
        else:
            if auth.get("id_token"):
                params["id_token_hint"] = auth["id_token"]
            if auth.get("email"):
                params["login_hint"] = auth["email"]
        return auth["authorize_url"] + "?" + urlencode(params), pending
    return CHATGPT_ISSUER + "/oauth/authorize?" + urlencode(params), pending


# LLM: 授权码只兑换一次、失败不重试；SIWC先验签绑定ID令牌，旧Codex继续原元数据提取。
# 函数用途: 用浏览器带回的授权码和本次 PKCE 校验码换取令牌；会向授权服务器发一次请求。
def exchange_browser_code(auth: dict, pending: dict, code: str) -> dict:
    body = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
        "client_id": pending.get("issued_client_id", auth["client_id"]), "code_verifier": pending["code_verifier"],
    }
    if is_siwc(auth):
        body["resource"] = SIWC_BASE
    status, obj = oauth_post(auth["token_url"], body)
    if status != 200:
        error = obj.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if is_siwc(auth) and code == "invalid_grant":
            raise ModelProfileError("授权码已失效，请用本次已登记的账号重新授权。", reason="authorization_code_invalid")
        raise ModelProfileError(f"授权码兑换失败（HTTP {status}），请重新登录。")
    if is_siwc(auth):
        return token_fields(obj, {**_without_credentials(auth), "client_id": body["client_id"]},
                            nonce=pending["nonce"], require_id=True)
    return token_fields(obj, _without_credentials(auth))


# LLM: 只做编码，不含随机或秘密判断；state、校验码与挑战值共用，改动会让授权服务器校验挑战失败。
# 函数用途: 按 RFC 7636 生成不带填充的 base64url 串。
def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# LLM: 刷新只由实际请求触发，失败不回退 API Key、不换模型，也不把 token 放到异常正文。
# 函数用途: 更新到期访问令牌，支持 refresh token 轮换。
def refresh_tokens(auth: dict) -> dict:
    if not auth.get("refresh_token"):
        raise ModelProfileError("登录已过期且没有刷新凭据，请在 /model 重新登录。")
    body = {"client_id": auth["client_id"], "grant_type": "refresh_token", "refresh_token": auth["refresh_token"]}
    if auth.get("client_secret"):
        body["client_secret"] = auth["client_secret"]
    if is_siwc(auth):
        body["resource"] = SIWC_BASE
    status, obj = oauth_post(auth["token_url"], body, as_json=auth["mode"] == "chatgpt" and not is_siwc(auth))
    if status != 200:
        error = obj.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if is_siwc(auth) and code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                                     "refresh_token_invalidated", "refresh_token_reused"}:
            raise ModelProfileError("刷新凭据已失效，请通过 Continue with ChatGPT 重新授权。", reason="credential_invalid")
        raise ModelProfileError(f"登录凭据刷新失败（HTTP {status}），未更换模型；请在 /model 检查登录。")
    return token_fields(obj, auth)


# LLM: 撤销请求只消费本次快照，调用方在配置锁外执行；网络失败不阻止本地退出，不记录服务端正文。
# 函数用途: 尝试结束可续期会话，返回确认状态；没有可撤销令牌时返回 None。
def revoke_session(auth: dict) -> bool | None:
    if not auth.get("refresh_token") or not auth.get("revocation_url"):
        return None
    try:
        status, _obj = oauth_post(auth["revocation_url"], {"token": auth["refresh_token"],
            "token_type_hint": "refresh_token", "client_id": auth["client_id"]}, allow_empty=True)
        return status == 200
    except ModelProfileError:
        return False


# LLM: OIDC发现和JWKS仅从固定签发方及同源地址读取；拒绝重定向，响应大小、连接时间均有界。
# 函数用途: 读取公开签名元数据，不携带账号令牌，不输出服务器正文。
def _auth_document(url: str) -> dict:
    auth_url(url)
    try:
        try:
            response = build_opener(NoAuthRedirect).open(Request(url, headers={"Accept": "application/json"}), timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            status, raw = response.code, response.read(262145)
        value = json.loads(raw) if status == 200 and len(raw) <= 262144 else None
        if not isinstance(value, dict):
            raise ValueError("shape")
        return value
    except (HTTPException, URLError, OSError, ValueError):
        raise ModelProfileError("无法取得 OpenAI 的公开签名资料；未采用本次登录凭据，请稍后重试。") from None


# LLM: 只允许文档指定签发方发现出的同源JWKS；每次验证使用当次有界读取，不信任JWT里提供的URL或密钥。
# 函数用途: 找到准确签名编号对应的RSA公钥，缺失、重复或形状不符时拒绝。
def _siwc_public_key(kid: str):
    from urllib.parse import urlsplit

    from cryptography.hazmat.primitives.asymmetric import rsa

    discovered = _auth_document(CHATGPT_ISSUER + "/.well-known/openid-configuration")
    if discovered.get("issuer") != CHATGPT_ISSUER:
        raise ValueError("issuer")
    uri = auth_url(discovered.get("jwks_uri"))
    source, target = urlsplit(CHATGPT_ISSUER), urlsplit(uri)
    if (source.scheme, source.netloc) != (target.scheme, target.netloc):
        raise ValueError("jwks origin")
    keys = _auth_document(uri).get("keys")
    if not isinstance(keys, list) or len(keys) > 128:
        raise ValueError("keys")
    matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
    if len(matching) != 1:
        raise ValueError("key identity")
    key = matching[0]
    if (key.get("kty") != "RSA" or key.get("use", "sig") != "sig" or key.get("alg", "RS256") != "RS256"
            or "key_ops" in key and key["key_ops"] != ["verify"]):
        raise ValueError("key algorithm")
    modulus = int.from_bytes(_jwt_segment(key["n"]), "big")
    exponent = int.from_bytes(_jwt_segment(key["e"]), "big")
    if not 2048 <= modulus.bit_length() <= 8192 or exponent.bit_length() > 32:
        raise ValueError("key size")
    return rsa.RSAPublicNumbers(exponent, modulus).public_key()


# LLM: JWT段必须是有界base64url，不容忍其它编码或空白；这里只解码，不授予身份。
# 函数用途: 解码JWT和JWK的一个传输段。
def _jwt_segment(value: object) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 65536:
        raise ValueError("segment")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


# LLM: 复用已有secrets extra，不自动安装或降级验签；授权页打开前即暴露准确的缺依赖原因。
# 函数用途: 确认SIWC验签原语可用，不读取凭据或联网。
def require_siwc_verifier() -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        raise ModelProfileError("Continue with ChatGPT 需要已有的 secrets 验签组件；请安装 my-agent[secrets] 后重试，未采用本次登录凭据。") from None


# LLM: 先用公开JWKS验证RS256签名，再核issuer/audience/expiry/nonce/subject；返回账号必须与原登记相同。
# 函数用途: 验证SIWC的ID令牌；失败仅报告固定提示，不包含JWT、邮箱或上游正文。
def _siwc_identity(token: object, auth: dict, *, nonce: str | None) -> dict:
    require_siwc_verifier()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        if not isinstance(token, str) or len(token) > 65536:
            raise ValueError("token")
        header_part, claim_part, signature_part = token.split(".")
        header, claims = json.loads(_jwt_segment(header_part)), json.loads(_jwt_segment(claim_part))
        if (not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str) or not header["kid"] or header.get("crit")):
            raise ValueError("header")
        key = _siwc_public_key(header["kid"])
        key.verify(_jwt_segment(signature_part), (header_part + "." + claim_part).encode("ascii"),
                   padding.PKCS1v15(), hashes.SHA256())
        audience, client = claims.get("aud"), auth["client_id"]
        audience_ok = audience == client or (isinstance(audience, list) and client in audience
            and all(isinstance(item, str) for item in audience) and (len(audience) == 1 or claims.get("azp") == client))
        expiry, subject = claims.get("exp"), claims.get("sub")
        if (claims.get("iss") != CHATGPT_ISSUER or not audience_ok
                or claims.get("azp", client) != client or isinstance(expiry, bool)
                or not isinstance(expiry, (float, int)) or not math.isfinite(expiry) or expiry <= time.time()
                or not isinstance(subject, str) or not subject or len(subject) > 8192):
            raise ValueError("identity")
        if nonce is not None and (not isinstance(claims.get("nonce"), str)
                or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode())):
            raise ValueError("nonce")
        if auth.get("subject") and auth["subject"] != subject:
            raise ValueError("account changed")
        if claims.get("email") is not None and not isinstance(claims["email"], str):
            raise ValueError("email")
        return claims
    except ModelProfileError:
        raise
    except (InvalidSignature, KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise ModelProfileError("SIWC 身份令牌验证失败，未覆盖已保存的账号；请重新登录。") from None
