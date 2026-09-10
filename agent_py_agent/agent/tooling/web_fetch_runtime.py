
# LLM: 本模块是 web_fetch 的受限 HTTP 传输与正文格式化边界；响应解码、大小上限和 SSRF 逐跳校验必须保持在同一条主链。
# 模块用途: 安全抓取网页/API，解压常见 HTTP 正文，并把文本预览或二进制产物整理成统一工具结果。

from __future__ import annotations

import http.client
import io
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import zlib
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .cancellation import (
    ToolCancelled,
    cancellation_requested,
    register_cancellation_callback,
)
from .models import ToolHandlerOutcome
from .web_html_preview import is_html_response, visible_html_text
from .web_markdown import html_to_markdown

_MAX_BODY_CHARS = 1_000_000
_MIN_RESPONSE_PREVIEW_CHARS = 256
_TEXTUAL_CONTENT_MARKERS = (
    "text/",
    "json",
    "xml",
    "javascript",
    "x-www-form-urlencoded",
)
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"identity", "gzip", "x-gzip", "deflate"})


@dataclass(frozen=True)
class RawResponseParts:
    status: int
    headers: Any
    body: bytes
    url: str


@dataclass(frozen=True)
class FetchRawRequest:
    tool: str
    url: str
    method: str
    headers: dict[str, str]
    data: bytes | None
    timeout: int
    max_bytes: int = _MAX_BODY_CHARS


@dataclass(frozen=True)
class FetchFormatRequest:
    tool: str
    artifact_root: Path
    url: str
    response: RawResponseParts
    fmt: str
    max_chars: int
    cache_hit: bool


_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CROSS_ORIGIN_SAFE_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control", "content-language",
    "content-type", "if-match", "if-modified-since", "if-none-match", "if-unmodified-since",
    "pragma", "range", "user-agent",
})


@dataclass(frozen=True)
class PinResult:
    """resolve_pin 的结果:ip=网关校验并固定的连接 IP;error=网关拒绝(初始或某重定向目标不安全)。"""

    ip: str | None
    error: ToolHandlerOutcome | None


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    ip: str


@dataclass(frozen=True)
class _HopReq:
    target: _Target
    request: FetchRawRequest
    url: str
    body: bytes | None


@dataclass(frozen=True)
class _HopOutcome:
    redirect_to: str
    result: RawResponseParts | ToolHandlerOutcome | None
    status: int = 0


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到网关校验过的 IP,但 Host 头/TLS SNI 用原主机名——杜绝连接层重解析 DNS(防 rebinding/TOCTOU)。"""

    def __init__(self, target: _Target, timeout: int, context: ssl.SSLContext) -> None:
        super().__init__(target.host, target.port, timeout=timeout, context=context)
        self._pin_ip = target.ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: _Target, timeout: int) -> None:
        super().__init__(target.host, target.port, timeout=timeout)
        self._pin_ip = target.ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        if self._tunnel_host:
            self._tunnel()


def _target_for(url: str, pin_ip: str) -> _Target:
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return _Target(scheme, parts.hostname or "", port, pin_ip)


def _request_path(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _open_pinned(target: _Target, timeout: int) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return _PinnedHTTPSConnection(target, timeout, ssl.create_default_context())
    return _PinnedHTTPConnection(target, timeout)


def _redirect_target(resp: Any, url: str) -> str:
    if resp.status not in _REDIRECT_STATUSES:
        return ""
    location = resp.getheader("Location") or ""
    return urljoin(url, location) if location else ""


def _network_failure(tool: str, exc: BaseException) -> ToolHandlerOutcome:
    if isinstance(exc, ToolCancelled) or cancellation_requested():
        return ToolHandlerOutcome(
            tool,
            False,
            "CANCELLED: HTTP 请求已取消。",
            error_code="CANCELLED",
        )
    if isinstance(exc, TimeoutError):
        # 网络失败带可重试错误码;无码会 fallback UNKNOWN_ERROR 误导模型"放弃报阻塞"(实测暴露)。
        return ToolHandlerOutcome(tool, False, f"请求超时: {exc.__class__.__name__}", error_code="TOOL_TIMEOUT")
    return ToolHandlerOutcome(tool, False, f"请求失败: {exc.__class__.__name__}", error_code="NETWORK_REQUEST_FAILED")


# LLM: 每跳保持 IP pin/SSRF 校验；跨 origin 只转发安全头，方法与正文按重定向状态转换。
# 函数用途: 发起有界 HTTP 抓取，关闭重定向连接而不下载无用正文。
def fetch_raw_response(request: FetchRawRequest, *, format_http_error, resolve_pin) -> RawResponseParts | ToolHandlerOutcome:
    """逐跳:每个 URL(含每个重定向目标)先过 resolve_pin(网关校验+取固定 IP),再 pin 到该 IP 连接。

    杜绝 SSRF 两条绕过:(1) DNS rebinding/TOCTOU——不让连接层重解析,连的就是网关校验过的 IP;
    (2) 重定向绕过——每个 3xx 目标重新过网关,不安全则拒,绝不盲目跟随到内网/云 metadata。
    """
    url, body = request.url, request.data
    deadline = time.monotonic() + max(0.0, float(request.timeout))
    for _hop in range(_MAX_REDIRECTS + 1):
        if cancellation_requested():
            return _network_failure(request.tool, ToolCancelled("cancelled"))
        pin = resolve_pin(url)
        if pin.error is not None:
            return pin.error  # 初始或某重定向目标没过网关
        if not pin.ip:
            return ToolHandlerOutcome(request.tool, False, "主机解析失败", error_code="NETWORK_REQUEST_FAILED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _network_failure(request.tool, TimeoutError())
        request = replace(request, timeout=remaining)
        outcome = _do_hop(_HopReq(_target_for(url, pin.ip), request, url, body), format_http_error)
        if outcome.redirect_to:
            request = _redirect_request(request, url, outcome.redirect_to, outcome.status)
            url, body = request.url, request.data
            continue
        return outcome.result
    return ToolHandlerOutcome(request.tool, False, "重定向次数过多", error_code="TOO_MANY_REDIRECTS")


# LLM: 重定向连接不会复用，不得无界 drain 响应体；关闭连接仍需覆盖取消与异常路径。
# 函数用途: 执行一跳并返回结构化状态，或读取受大小上限保护的最终正文。
def _do_hop(hop: _HopReq, format_http_error) -> _HopOutcome:
    conn: http.client.HTTPConnection | None = None
    try:
        conn, resp = _send_pinned(hop)
        with register_cancellation_callback(lambda: _abort_connection(conn)):
            if cancellation_requested():
                raise ToolCancelled("cancelled")
            location = _redirect_target(resp, hop.url)
            if location:
                return _HopOutcome(location, None, resp.status)
            result = _finalize_hop(hop, resp, format_http_error)
            if _connection_timed_out(conn):
                raise TimeoutError()
            return _HopOutcome("", result)
    except (ToolCancelled, TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        # 连接或读取被取消/超时/断流时使用同一结构化网络结果。
        return _HopOutcome("", _network_failure(hop.request.tool, TimeoutError() if _connection_timed_out(conn) else exc))
    finally:
        if conn is not None:
            timer = getattr(conn, "_request_deadline_timer", None)
            if timer is not None:
                timer.cancel()
            conn.close()


# LLM: deadline 是整次请求预算，不是每次 recv 的空闲超时；关闭 socket 才能打断持续滴流和响应文件对象。
# 函数用途: 终止底层 HTTP I/O；关闭只作用于本次连接，不代表远端回滚请求。
def _abort_connection(conn) -> None:
    sock = getattr(conn, "_request_socket", None) or getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


# LLM: 超时标记由宿主 Timer 设置，不能从 HTTP 响应正文或报错文案推断。
# 函数用途: 判断当前连接是否已耗尽请求总预算。
def _connection_timed_out(conn) -> bool:
    event = getattr(conn, "_request_deadline_expired", None)
    return event is not None and event.is_set() is True


# LLM: timer 与 socket timeout 共用上层剩余预算；释放 timer 的责任属于最终响应或发送失败路径。
# 函数用途: 为一条连接启动总超时中断，阻止服务端持续少量发字节拖长请求。
def _start_request_deadline(conn, timeout: float) -> None:
    event = threading.Event()
    conn._request_deadline_expired = event
    # LLM: 回调只设置宿主超时事实并中断当前连接，不修改工具账本。
    # 函数用途: 到期唤醒正在等待头部或正文的请求线程。
    def expire():
        event.set()
        _abort_connection(conn)
    timer = threading.Timer(max(0.0, float(timeout)), expire)
    timer.daemon = True
    conn._request_deadline_timer = timer
    timer.start()


# LLM: 重定向原点包含 scheme/host/port；未知自定义头不能因换域而泄露，307/308 不得丢请求体。
# 函数用途: 按 HTTP 重定向语义产生下一跳请求；不修改调用者原始配置。
def _redirect_request(request: FetchRawRequest, source: str, target: str, status: int) -> FetchRawRequest:
    old, new = _target_for(source, ""), _target_for(target, "")
    same_origin = (old.scheme, old.host, old.port) == (new.scheme, new.host, new.port)
    headers = {k: v for k, v in request.headers.items()
               if same_origin or k.lower() in _CROSS_ORIGIN_SAFE_HEADERS}
    method, data = request.method, request.data
    if status == 303 or (status in {301, 302} and method == "POST"):
        method = "HEAD" if method == "HEAD" else "GET"
        data = None
        headers = {k: v for k, v in headers.items()
                   if k.lower() not in {"content-type", "content-length", "transfer-encoding", "content-encoding"}}
    return replace(request, url=target, method=method, headers=headers, data=data)


# LLM: Host 默认值必须包含非默认端口与 IPv6 方括号；显式首跳 header 不在这里改写。
# 函数用途: 为固定 IP 连接生成原 URL 的 HTTP authority。
def _target_authority(target: _Target) -> str:
    host = f"[{target.host}]" if ":" in target.host else target.host
    default_port = 443 if target.scheme == "https" else 80
    return host if target.port == default_port else f"{host}:{target.port}"


# LLM: 请求头在这里完成协议级默认值；不得覆盖调用者显式 header，也不得绕过固定 IP 连接。
# 函数用途: 向校验过的目标 IP 发起一次可取消请求，并声明本底座能够安全解压的响应编码。
def _send_pinned(hop: _HopReq) -> tuple[http.client.HTTPConnection, Any]:

    conn = _open_pinned(hop.target, hop.request.timeout)
    _start_request_deadline(conn, hop.request.timeout)
    try:
        with register_cancellation_callback(lambda: _abort_connection(conn)):
            if cancellation_requested():
                raise ToolCancelled("cancelled")
            headers = dict(hop.request.headers)
            _setdefault_header(headers, "Host", _target_authority(hop.target))
            _setdefault_header(headers, "Accept-Encoding", "gzip, deflate")
            conn.request(
                hop.request.method,
                _request_path(hop.url),
                body=hop.body,
                headers=headers,
            )
            conn._request_socket = getattr(conn, "sock", None)
            return conn, conn.getresponse()
    except BaseException:
        conn._request_deadline_timer.cancel()
        conn.close()
        if _connection_timed_out(conn):
            raise TimeoutError() from None
        raise


# LLM: HTTP header 名称不区分大小写；新增默认头时必须使用本入口，避免生成语义重复的大小写变体。
# 函数用途: 仅在调用者没有提供同名请求头时加入默认值。
def _setdefault_header(headers: dict[str, str], name: str, value: str) -> None:
    lowered = name.lower()
    if any(str(existing).lower() == lowered for existing in headers):
        return
    headers[name] = value


# LLM: 读取上限同时约束传输体和解压后的实体体；任何编码失败都必须成为结构化工具失败，不能把压缩字节替换解码进模型上下文。
# 函数用途: 读取并按 Content-Encoding 解压一次 HTTP 响应，超过上限、编码不支持或正文损坏时给出明确错误。
def _finalize_hop(hop: _HopReq, resp: Any, format_http_error) -> RawResponseParts | ToolHandlerOutcome:
    body = resp.read(hop.request.max_bytes + 1)
    if len(body) > hop.request.max_bytes:
        return ToolHandlerOutcome(hop.request.tool, False, f"响应体过大，最多 {hop.request.max_bytes} 字节", error_code="ARTIFACT_TOO_LARGE")
    try:
        body = _decode_response_body(body, resp.headers, hop.request.max_bytes)
    except OverflowError:
        return ToolHandlerOutcome(
            hop.request.tool,
            False,
            f"响应解压后过大，最多 {hop.request.max_bytes} 字节",
            error_code="ARTIFACT_TOO_LARGE",
        )
    except LookupError as exc:
        return ToolHandlerOutcome(
            hop.request.tool,
            False,
            f"响应使用了不支持的 Content-Encoding: {exc}",
            error_code="NETWORK_REQUEST_FAILED",
        )
    except ValueError:
        return ToolHandlerOutcome(
            hop.request.tool,
            False,
            "响应正文压缩数据损坏，无法安全解码",
            error_code="NETWORK_REQUEST_FAILED",
        )
    if resp.status >= 400:
        err = urllib.error.HTTPError(hop.url, resp.status, resp.reason or "", resp.headers, io.BytesIO(body))
        return format_http_error(hop.request.tool, err, _MIN_RESPONSE_PREVIEW_CHARS)
    return RawResponseParts(resp.status, resp.headers, body, hop.url)


# LLM: Content-Encoding 按服务端应用顺序的逆序解码，并在每一层维持同一个实体大小硬上限。
# 函数用途: 解开 gzip/deflate HTTP 正文；空正文和 identity 原样返回，未知编码明确拒绝。
def _decode_response_body(body: bytes, headers: Any, max_bytes: int) -> bytes:
    if not body:
        return body
    raw_encoding = _header_value(headers, "Content-Encoding")
    encodings = tuple(part.strip().lower() for part in raw_encoding.split(",") if part.strip())
    decoded = body
    for encoding in reversed(encodings):
        if encoding == "identity":
            continue
        if encoding not in _SUPPORTED_CONTENT_ENCODINGS:
            raise LookupError(encoding)
        if encoding in {"gzip", "x-gzip"}:
            decoded = _inflate_limited(decoded, 16 + zlib.MAX_WBITS, max_bytes)
            continue
        try:
            decoded = _inflate_limited(decoded, zlib.MAX_WBITS, max_bytes)
        except ValueError:
            decoded = _inflate_limited(decoded, -zlib.MAX_WBITS, max_bytes)
    return decoded


# LLM: HTTPMessage 与测试字典都可能承载响应头；查找必须保持 RFC 大小写不敏感语义。
# 函数用途: 从任意响应头容器中读取指定字段，兼容普通字典的不同大小写。
def _header_value(headers: Any, name: str) -> str:
    value = headers.get(name, "") if hasattr(headers, "get") else ""
    if value not in (None, ""):
        return str(value)
    lowered = name.lower()
    items = headers.items() if hasattr(headers, "items") else ()
    for key, candidate in items:
        if str(key).lower() == lowered:
            return str(candidate or "")
    return ""


# LLM: zlib 的 max_length 和 eof 状态共同构成解压炸弹/截断数据边界；不得改回一次性 decompress。
# 函数用途: 在固定字节预算内解压一层 gzip/deflate 数据，并区分超限和损坏两类失败。
def _inflate_limited(body: bytes, window_bits: int, max_bytes: int) -> bytes:
    decoder = zlib.decompressobj(window_bits)
    try:
        decoded = decoder.decompress(body, max_bytes + 1)
        if len(decoded) > max_bytes or decoder.unconsumed_tail:
            raise OverflowError
        decoded += decoder.flush(max_bytes + 1 - len(decoded))
    except zlib.error as exc:
        raise ValueError("invalid compressed response") from exc
    if len(decoded) > max_bytes:
        raise OverflowError
    if not decoder.eof:
        raise ValueError("truncated compressed response")
    return decoded


# LLM: 完整文本必须留在 outcome.output 交给统一外置层；max_chars 只形成模型预览，不能在 handler 内永久丢掉尾部。
# 函数用途: 把已解码响应格式化为完整工具正文，并在正文较长时附上可恢复的短预览策略。
def format_fetch_result(request: FetchFormatRequest) -> ToolHandlerOutcome:
    content_type = content_type_from_headers(request.response.headers)
    if not is_textual_response(request.response.headers):
        artifact = save_web_artifact(
            request.artifact_root,
            url=request.url,
            body=request.response.body,
            content_type=content_type,
        )
        return ToolHandlerOutcome(
            request.tool,
            True,
            f"status={request.response.status}\ncontent_type={content_type}\nbytes={len(request.response.body)}\nartifact_ref={artifact['path']}",
            result_envelope={"artifact": artifact, "cache": {"hit": request.cache_hit}},
        )
    body = request.response.body.decode("utf-8", "replace")
    output_format = effective_fetch_format(request.fmt, request.response.headers)
    content = content_for_format(output_format, body, request.response.headers)
    truncated = len(content) > request.max_chars
    full_output = (
        f"status={request.response.status}\n"
        f"content_type={content_type}\n"
        f"format={output_format}\n"
        "truncated=false\n\n"
        f"{content}"
    )
    envelope: dict[str, Any] = {
        "cache": {"hit": request.cache_hit},
        "format": output_format,
        "url": request.url,
        "preview": {
            "truncated": truncated,
            "max_chars": request.max_chars,
            "full_chars": len(content),
        },
    }
    if truncated:
        live_output = (
            f"status={request.response.status}\n"
            f"content_type={content_type}\n"
            f"format={output_format}\n"
            "truncated=true\n\n"
            f"{content[:request.max_chars]}\n... 已截断"
        )
        envelope["tool_output_policy"] = {
            "live_prompt_output": live_output,
            "requires_recovery_artifact": True,
        }
    return ToolHandlerOutcome(
        request.tool,
        True,
        full_output,
        result_envelope=envelope,
    )


def page_payload_from_response(
    artifact_root: Path,
    url: str,
    response: RawResponseParts,
    max_chars: int,
) -> dict[str, Any]:
    content_type = content_type_from_headers(response.headers)
    if is_textual_response(response.headers):
        body = response.body.decode("utf-8", "replace")
        text = visible_html_text(body) if is_html_response(response.headers) else body
        title = extract_html_title(body) if is_html_response(response.headers) else ""
        artifact_bytes = text.encode("utf-8")
        artifact_type = "text/plain"
    else:
        title = ""
        artifact_bytes = response.body
        artifact_type = content_type
        text = ""
    artifact = save_web_artifact(artifact_root, url=url, body=artifact_bytes, content_type=artifact_type)
    return {
        "url": url,
        "status": response.status,
        "title": title,
        "content_type": content_type,
        "preview": text[:max_chars],
        "artifact_ref": artifact["path"],
        "content_hash": artifact["sha256"],
        "bytes": artifact["bytes"],
    }


def normalize_fetch_format(value: Any) -> str:
    fmt = str(value or "auto").strip().lower()
    if fmt not in {"auto", "markdown", "text", "html", "json", "raw"}:
        raise ValueError("format/mode 只支持 auto、markdown、text、html、json、raw、extract")
    return fmt


def normalize_url_list(value: Any, normalize_url) -> list[str]:
    if isinstance(value, str):
        raw_urls = [value]
    elif isinstance(value, list):
        raw_urls = value
    else:
        raise ValueError("urls 必须是 URL 字符串或 URL 数组")
    urls = [normalize_url(item) for item in raw_urls]
    if not urls:
        raise ValueError("urls 不能为空")
    return list(dict.fromkeys(urls))


def content_for_format(output_format: str, body: str, headers: Any) -> str:
    if output_format == "markdown":
        return html_to_markdown(body) if is_html_response(headers) else body
    if output_format == "text":
        return visible_html_text(body) if is_html_response(headers) else body
    if output_format in {"json", "raw"}:
        return body
    return body


def content_type_from_headers(headers: Any) -> str:
    return str(headers.get("Content-Type", "")).lower()


def is_textual_response(headers: Any) -> bool:
    content_type = content_type_from_headers(headers)
    return any(marker in content_type for marker in _TEXTUAL_CONTENT_MARKERS)


def effective_fetch_format(fmt: str, headers: Any) -> str:
    if fmt != "auto":
        return fmt
    return "markdown" if is_html_response(headers) else "text"


def default_artifact_root() -> Path:
    raw = os.environ.get("MY_AGENT_WEB_ARTIFACT_ROOT", "")
    if raw.strip():
        return Path(raw).expanduser()
    home = os.environ.get("MY_AGENT_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".my-agent"
    return root / "artifacts" / "web"


def save_web_artifact(root: Path, *, url: str, body: bytes, content_type: str) -> dict[str, Any]:
    digest = sha256(body).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest[:16]}{artifact_extension(content_type)}"
    path.write_bytes(body)
    return {
        "path": str(path),
        "sha256": digest,
        "bytes": len(body),
        "content_type": content_type,
        "url": url,
    }


def artifact_extension(content_type: str) -> str:
    if content_type.startswith("text/"):
        return ".txt"
    for marker, extension in (
        ("pdf", ".pdf"),
        ("json", ".json"),
        ("html", ".html"),
        ("xml", ".xml"),
        ("csv", ".csv"),
        ("png", ".png"),
    ):
        if marker in content_type:
            return extension
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    return ".bin"


def extract_html_title(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.IGNORECASE | re.DOTALL)
    if heading:
        return re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", heading.group(1))).strip()
    return ""


__all__ = [
    "RawResponseParts",
    "FetchFormatRequest",
    "FetchRawRequest",
    "default_artifact_root",
    "fetch_raw_response",
    "format_fetch_result",
    "normalize_fetch_format",
    "normalize_url_list",
    "page_payload_from_response",
]
