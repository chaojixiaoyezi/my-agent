
from __future__ import annotations

import http.client
import io
import os
import re
import socket
import ssl
import urllib.error
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .models import ToolExecutionResult
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


@dataclass(frozen=True)
class PinResult:
    """resolve_pin 的结果:ip=网关校验并固定的连接 IP;error=网关拒绝(初始或某重定向目标不安全)。"""

    ip: str | None
    error: ToolExecutionResult | None


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
    result: RawResponseParts | ToolExecutionResult | None


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


def _network_failure(tool: str, exc: BaseException) -> ToolExecutionResult:
    if isinstance(exc, TimeoutError):
        # 网络失败带可重试错误码;无码会 fallback UNKNOWN_ERROR 误导模型"放弃报阻塞"(实测暴露)。
        return ToolExecutionResult(tool, False, f"请求超时: {exc.__class__.__name__}", error_code="TOOL_TIMEOUT")
    return ToolExecutionResult(tool, False, f"请求失败: {exc.__class__.__name__}", error_code="NETWORK_REQUEST_FAILED")


def fetch_raw_response(request: FetchRawRequest, *, format_http_error, resolve_pin) -> RawResponseParts | ToolExecutionResult:
    """逐跳:每个 URL(含每个重定向目标)先过 resolve_pin(网关校验+取固定 IP),再 pin 到该 IP 连接。

    杜绝 SSRF 两条绕过:(1) DNS rebinding/TOCTOU——不让连接层重解析,连的就是网关校验过的 IP;
    (2) 重定向绕过——每个 3xx 目标重新过网关,不安全则拒,绝不盲目跟随到内网/云 metadata。
    """
    url, body = request.url, request.data
    for _hop in range(_MAX_REDIRECTS + 1):
        pin = resolve_pin(url)
        if pin.error is not None:
            return pin.error  # 初始或某重定向目标没过网关
        if not pin.ip:
            return ToolExecutionResult(request.tool, False, "主机解析失败", error_code="NETWORK_REQUEST_FAILED")
        outcome = _do_hop(_HopReq(_target_for(url, pin.ip), request, url, body), format_http_error)
        if outcome.redirect_to:
            url, body = outcome.redirect_to, None
            continue
        return outcome.result
    return ToolExecutionResult(request.tool, False, "重定向次数过多", error_code="TOO_MANY_REDIRECTS")


def _do_hop(hop: _HopReq, format_http_error) -> _HopOutcome:
    try:
        conn, resp = _send_pinned(hop)
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        return _HopOutcome("", _network_failure(hop.request.tool, exc))
    try:
        location = _redirect_target(resp, hop.url)
        if location:
            resp.read()  # 排空再换下一跳
            return _HopOutcome(location, None)
        return _HopOutcome("", _finalize_hop(hop, resp, format_http_error))
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        # 连接成功后读取 response body 仍可能超时/断流；它和 connect/request 阶段
        # 使用同一结构化网络错误，不能逃到 harvester 外层打印整段 traceback。
        return _HopOutcome("", _network_failure(hop.request.tool, exc))
    finally:
        conn.close()


def _send_pinned(hop: _HopReq) -> tuple[Any, Any]:
    conn = _open_pinned(hop.target, hop.request.timeout)
    headers = dict(hop.request.headers)
    headers.setdefault("Host", hop.target.host)
    conn.request(hop.request.method, _request_path(hop.url), body=hop.body, headers=headers)
    return conn, conn.getresponse()


def _finalize_hop(hop: _HopReq, resp: Any, format_http_error) -> RawResponseParts | ToolExecutionResult:
    body = resp.read(hop.request.max_bytes + 1)
    if len(body) > hop.request.max_bytes:
        return ToolExecutionResult(hop.request.tool, False, f"响应体过大，最多 {hop.request.max_bytes} 字节", error_code="ARTIFACT_TOO_LARGE")
    if resp.status >= 400:
        err = urllib.error.HTTPError(hop.url, resp.status, resp.reason or "", resp.headers, io.BytesIO(body))
        return format_http_error(hop.request.tool, err, _MIN_RESPONSE_PREVIEW_CHARS)
    return RawResponseParts(resp.status, resp.headers, body, hop.url)


def format_fetch_result(request: FetchFormatRequest) -> ToolExecutionResult:
    content_type = content_type_from_headers(request.response.headers)
    if not is_textual_response(request.response.headers):
        artifact = save_web_artifact(
            request.artifact_root,
            url=request.url,
            body=request.response.body,
            content_type=content_type,
        )
        return ToolExecutionResult(
            request.tool,
            True,
            f"status={request.response.status}\ncontent_type={content_type}\nbytes={len(request.response.body)}\nartifact_ref={artifact['path']}",
            result_envelope={"artifact": artifact, "cache": {"hit": request.cache_hit}},
        )
    body = request.response.body.decode("utf-8", "replace")
    output_format = effective_fetch_format(request.fmt, request.response.headers)
    content = content_for_format(output_format, body, request.response.headers)
    truncated = len(content) > request.max_chars
    output = (
        f"status={request.response.status}\n"
        f"content_type={content_type}\n"
        f"format={output_format}\n"
        f"truncated={str(truncated).lower()}\n\n"
        f"{content[:request.max_chars]}"
    )
    if truncated:
        output += "\n... 已截断"
    return ToolExecutionResult(
        request.tool,
        True,
        output,
        result_envelope={"cache": {"hit": request.cache_hit}, "format": output_format, "url": request.url},
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
