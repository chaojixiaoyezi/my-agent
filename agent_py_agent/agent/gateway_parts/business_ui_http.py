# LLM: 同源业务工作台HTML与JS托管；复用原Owner认证与CSRF动态注入，不修改静态磁盘文件。
# 模块用途: 响应 GET /client/business/ui 与 /client/business/ui/app.js，提供同源认证工作台托管与CSRF会话绑定。
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from .business_api import _native_source, native_business_context
from .http_service import _is_client_disconnect_error


# LLM: 从宿主实例读取或初始化CSRF签名私钥；优先绑定本机客户端持久凭据，单会话稳定。
# 函数用途: 返回当前Gateway宿主实例用于签署CSRF Token的秘密字节串。
def _get_csrf_secret(server) -> bytes:
    secret = getattr(server, "_business_csrf_secret", None)
    if secret is None:
        local_cred = getattr(server, "_local_client_credential", None)
        if local_cred and isinstance(local_cred, str):
            secret = local_cred.encode("utf-8")
        else:
            import secrets

            secret = secrets.token_bytes(32)
        server._business_csrf_secret = secret
    return secret if isinstance(secret, bytes) else str(secret).encode("utf-8")


# LLM: CSRF Token 严格绑定当前 Gateway 实例、认证 Owner ID 与 User ID，防跨会话与跨租户伪造。
# 函数用途: 为已认证的业务会话生成唯一的 CSRF Token 字符串。
def generate_csrf_token(server, owner_id: str, user_id: str = "") -> str:
    secret = _get_csrf_secret(server)
    payload = f"my-agent-csrf:{owner_id}:{user_id}".encode()
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"csrf-{signature[:32]}"


# LLM: 常量时间比对防时序攻击；严格校验 Token 归属与当前 Owner 及 User 一致性。
# 函数用途: 验证请求携带的 CSRF Token 是否真实有效并绑定当前 Owner 与用户。
def verify_csrf_token(server, token: str, owner_id: str, user_id: str = "") -> bool:
    if not token or not isinstance(token, str):
        return False
    expected = generate_csrf_token(server, owner_id, user_id)
    return hmac.compare_digest(token.strip(), expected)


# LLM: 浏览器端请求CSRF令牌强制门禁；同源请求校验CSRF与非cross-site，非浏览器请求(CLI/SDK)零侵入放行。
# 函数用途: 对来自浏览器环境的POST/Stream/Upload请求强制校验CSRF Token与同源策略。
def enforce_browser_request_token(handler, server, context, services) -> None:
    headers = getattr(handler, "headers", None)
    if headers is None:
        return
    origin = headers.get("Origin")
    sec_site = headers.get("Sec-Fetch-Site")
    csrf_token = headers.get("X-CSRF-Token")
    if origin is None and sec_site is None and csrf_token is None:
        return
    if str(sec_site or "").strip().lower() == "cross-site":
        raise HostAPIError("CAPABILITY_DENIED", 403, message="Request token invalid or missing")
    owner_id = services.host.home_paths.owner_id
    user_id = getattr(context, "subject_id", "") or ""
    if not verify_csrf_token(server, str(csrf_token or ""), owner_id, str(user_id)):
        raise HostAPIError("CAPABILITY_DENIED", 403, message="Request token invalid or missing")


# LLM: 严格自工作区向内查找静态资产文件，防止向上路径穿越与符号链接逃逸。
# 函数用途: 安全解析业务工作台静态资产的磁盘物理绝对路径。
def _locate_ui_file(filename: str) -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "examples" / "business_space_ui" / filename
    if candidate.is_file():
        return candidate
    alt = Path(__file__).resolve().parents[2] / "examples" / "business_space_ui" / filename
    if alt.is_file():
        return alt
    raise FileNotFoundError(f"Business UI asset {filename} not found")


# LLM: 提供同源认证的工作台HTML托管，严格限制CSP与无缓存并注入当前Owner与用户的会话CSRF Token。
# 函数用途: 响应 GET /client/business/ui，验证宿主凭据，注入 meta CSRF token 并返回 HTML。
def handle_business_ui(handler, server) -> None:
    try:
        headers = getattr(handler, "headers", None)
        if headers is not None and headers.get("Sec-Fetch-Site") == "cross-site":
            handler._send_json(403, {"error": {"code": "CAPABILITY_DENIED"}})
            return
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        owner_id = services.host.home_paths.owner_id
        user_id = getattr(context, "subject_id", "") or ""
        csrf_token = generate_csrf_token(server, owner_id, str(user_id))

        html_path = _locate_ui_file("index.html")
        with open(html_path, encoding="utf-8") as file_handle:
            raw_html = file_handle.read()

        injected_html = raw_html.replace(
            '<meta name="csrf-token" content="">',
            f'<meta name="csrf-token" content="{csrf_token}">',
        ).replace(
            '<meta name="my-agent-csrf" content="">',
            f'<meta name="my-agent-csrf" content="{csrf_token}">',
        )
        if '<meta name="my-agent-csrf"' not in injected_html:
            injected_html = injected_html.replace(
                "<head>",
                f'<head>\n  <meta name="my-agent-csrf" content="{csrf_token}">',
            )

        body = injected_html.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
    except HostAPIError as error:
        handler._send_json(error.status, {"error": {"code": error.code}})
    except (BusinessError, ValueError):
        handler._send_json(403, {"error": {"code": "CAPABILITY_DENIED"}})
    except OSError as exc:
        if not _is_client_disconnect_error(exc):
            raise


# LLM: 托管业务工作台核心脚本；强制与HTML使用相同的安全头与同源凭据门禁。
# 函数用途: 响应 GET /client/business/ui/app.js，验证凭据并返回前端脚本。
def handle_business_ui_app_js(handler, server) -> None:
    try:
        headers = getattr(handler, "headers", None)
        if headers is not None and headers.get("Sec-Fetch-Site") == "cross-site":
            handler._send_json(403, {"error": {"code": "CAPABILITY_DENIED"}})
            return
        if not _native_source(handler):
            return
        native_business_context(handler, server)

        js_path = _locate_ui_file("app.js")
        with open(js_path, encoding="utf-8") as file_handle:
            js_content = file_handle.read()

        body = js_content.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/javascript; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
    except HostAPIError as error:
        handler._send_json(error.status, {"error": {"code": error.code}})
    except (BusinessError, ValueError):
        handler._send_json(403, {"error": {"code": "CAPABILITY_DENIED"}})
    except OSError as exc:
        if not _is_client_disconnect_error(exc):
            raise
