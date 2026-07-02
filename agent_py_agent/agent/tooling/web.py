
from __future__ import annotations

"""implements outbound HTTP tools behind explicit timeout and output-size limits.

这个文件只负责访问网络。
`web_fetch` 是唯一的 URL/API 读取入口：单页读取、批量抽取和简单 HTTP 请求都走它。
这里统一限制超时时间和返回长度，避免一次请求把主流程卡死或把 prompt 撑爆。
"""

import os
import socket
import urllib.error
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..contracts.gates.network_safety import (
    NetworkResolver,
    NetworkSafetyFacts,
    evaluate_network_safety_gate,
)
from .models import ToolExecutionResult
from .web_fetch_tools import WebFetchTool as _WebFetchTool
from .web_fetch_tools import WebRuntimeDeps
from .web_http_helpers import scalar_text

_MAX_URL_CHARS = 4096
_MIN_RESPONSE_PREVIEW_CHARS = 256


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


def _normalize_url(value: Any) -> str:
    url = scalar_text(value, name="url", max_chars=_MAX_URL_CHARS)
    if _has_control_chars(url):
        raise ValueError("url 包含不支持的控制字符")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("url 只支持 http 或 https")
    if not parts.netloc or not parts.hostname:
        raise ValueError("url 必须包含有效主机名")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url 不支持携带用户名或密码")
    return url


def _default_network_resolver(host: str) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return tuple(str(sockaddr[0]) for *_prefix, sockaddr in answers)


def _network_safety_error(
    tool: str,
    url: str,
    resolver: NetworkResolver,
    allowed_private_hosts: Iterable[str] = (),
    allow_private_resolution: bool | None = None,
) -> ToolExecutionResult | None:
    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            url=url,
            resolver=resolver,
            allowed_private_hosts=allowed_private_hosts,
            allow_private_resolution=_effective_allow_private_resolution(allow_private_resolution),
        )
    )
    if decision.allowed:
        return None
    code = decision.finding_codes[0] if decision.finding_codes else "NETWORK_SAFETY_DENIED"
    return ToolExecutionResult(
        tool,
        False,
        f"网络安全检查失败: {code}{_private_host_recovery_note(code, url)}",
        result_envelope={"network_safety_gate": decision.to_dict()},
        error_code=code,
    )


def _private_host_recovery_note(code: str, url: str) -> str:
    """私网拦截自带可执行的自纠通路(真机回归② N1:子代理撞闸后不知道有授权路,直接放弃拖崩
    编队)。只对私网类拦截追加;metadata/link-local 永久拦截等其余码保持原样(本就无路可走)。"""
    if code not in ("NETWORK_PRIVATE_HOST_BLOCKED", "NETWORK_PRIVATE_IP_BLOCKED"):
        return ""
    host = urllib.parse.urlsplit(url).hostname or ""
    return (
        f"(目标 {host} 是内网/私网地址,默认出站防护拦截——这是授权缺口,不是网络故障。"
        "若它正是用户任务指定的目标: 主代理→经用户确认后调 authorize_network_host(confirmed=true) "
        "把它加入白名单再重试;子代理→调 capability_request(capability_type=network, "
        f"network_scope=[\"{host}\"], requested_tools=[\"web_fetch\"]) 申请授权,等父代理处理期间"
        "继续其他可做的工作,不要因此放弃(abandon)整个任务。)"
    )


def _effective_allow_private_resolution(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("MY_AGENT_ALLOW_PRIVATE_URLS", "")
    return raw.strip().lower() in {"1", "true"}


def _format_http_error(tool: str, exc: urllib.error.HTTPError, max_chars: int) -> ToolExecutionResult:
    detail = exc.read(max_chars + 1).decode("utf-8", "replace")
    result = (
        f"HTTP {exc.code}\n"
        f"content_type={exc.headers.get('Content-Type', '')}\n\n"
        f"{detail[:max_chars]}"
    )
    if len(detail) > max_chars:
        result += "\n... 已截断"
    # HTTP 状态错误带明确码,否则无码→fallback UNKNOWN_ERROR 误导模型放弃。分流:
    #   5xx/408/429 服务器侧临时错误→可退避重试(NETWORK_REQUEST_FAILED);
    #   401/403 鉴权/授权失败→改 URL/参数也修不了，应走授权或换来源(PERMISSION_BLOCKED,
    #     不可重试);否则让模型反复改 URL/header 空转;
    #   其余 4xx(400/404/422 等)请求/URL 问题→改 URL/参数再试(TOOL_INVALID_ARGUMENTS)。
    #   404 不用 PATH_NOT_FOUND:其 recovery_hint 指向 list_files/candidate_paths 等文件系统语义,
    #     在 web 上下文会误导;改 URL(TOOL_INVALID_ARGUMENTS)才是 404 的正确恢复。
    if exc.code >= 500 or exc.code in (408, 429):
        code = "NETWORK_REQUEST_FAILED"
    elif exc.code in (401, 403):
        code = "PERMISSION_BLOCKED"
    else:
        code = "TOOL_INVALID_ARGUMENTS"
    return ToolExecutionResult(tool, False, result, error_code=code)


def _response_preview_chars(params: dict[str, Any], configured_max: int) -> int:
    raw = params.get("max_chars")
    if raw in (None, ""):
        return configured_max
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return configured_max
    return max(_MIN_RESPONSE_PREVIEW_CHARS, min(configured_max, requested))


class WebFetchTool(_WebFetchTool):
    def __init__(
        self,
        *,
        max_chars: int,
        timeout: int,
        resolver: NetworkResolver | None = None,
        artifact_root: Path | None = None,
        cache_ttl_seconds: int = 900,
        allowed_private_hosts: Iterable[str] = (),
        allow_private_resolution: bool | None = None,
    ):
        super().__init__(WebRuntimeDeps(
            max_chars=max_chars,
            timeout=timeout,
            resolver=resolver or _default_network_resolver,
            normalize_url=_normalize_url,
            response_preview_chars=_response_preview_chars,
            network_safety_error=_network_safety_error,
            format_http_error=_format_http_error,
            artifact_root=artifact_root,
            cache_ttl_seconds=cache_ttl_seconds,
            allowed_private_hosts=tuple(str(item) for item in allowed_private_hosts),
            allow_private_resolution=allow_private_resolution,
        ))
