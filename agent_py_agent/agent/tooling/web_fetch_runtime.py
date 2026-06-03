
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

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


def fetch_raw_response(request: FetchRawRequest, *, format_http_error) -> RawResponseParts | ToolExecutionResult:
    req = urllib.request.Request(
        request.url,
        data=request.data,
        method=request.method,
        headers=request.headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            return _raw_response_from_http_response(request, resp)
    except urllib.error.HTTPError as exc:
        return format_http_error(request.tool, exc, _MIN_RESPONSE_PREVIEW_CHARS)
    except (urllib.error.URLError, TimeoutError) as exc:
        return ToolExecutionResult(request.tool, False, f"请求失败: {exc.__class__.__name__}")


def _raw_response_from_http_response(request: FetchRawRequest, resp: Any) -> RawResponseParts | ToolExecutionResult:
    body = resp.read(request.max_bytes + 1)
    if len(body) > request.max_bytes:
        return ToolExecutionResult(
            request.tool,
            False,
            f"响应体过大，最多 {request.max_bytes} 字节",
            error_code="ARTIFACT_TOO_LARGE",
        )
    return RawResponseParts(resp.status, resp.headers, body, request.url)


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
