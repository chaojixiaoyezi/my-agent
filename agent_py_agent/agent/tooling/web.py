# LLM: 网络访问边界和错误格式要稳定，避免外部请求拖垮主流程。
# 模块用途: HTTP 和网页抓取工具，统一 URL/header 校验、超时和响应截断。

from __future__ import annotations

"""implements outbound HTTP tools behind explicit timeout and output-size limits.

给人看的解释：
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

from ..contracts.gates import NetworkResolver, NetworkSafetyFacts, evaluate_network_safety_gate
from .models import ToolExecutionResult
from .web_fetch_tools import WebFetchTool as _WebFetchTool
from .web_fetch_tools import WebRuntimeDeps
from .web_http_helpers import scalar_text

_MAX_URL_CHARS = 4096
_MIN_RESPONSE_PREVIEW_CHARS = 256


# LLM: _has_control_chars catches invisible URL control bytes before network access.
# 函数用途: 判断 URL 文本是否包含控制字符，避免把坏输入交给请求层。
def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


# LLM: _normalize_url 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
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


# LLM: _default_network_resolver supplies DNS facts for the network_safety gate.
# 函数用途: 只解析 host 到 IP，不发起 HTTP 请求；失败由 network_safety gate 转成结构化错误。
def _default_network_resolver(host: str) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return tuple(str(sockaddr[0]) for *_prefix, sockaddr in answers)


# LLM: _network_safety_error converts SSRF/DNS gate denials into normal tool failures.
# 函数用途: 所有出站 HTTP 工具在请求前统一过 network_safety 门，避免只靠 URL 字面校验。
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
        f"网络安全检查失败: {code}",
        result_envelope={"network_safety_gate": decision.to_dict()},
        error_code=code,
    )


# LLM: _effective_allow_private_resolution keeps proxy/VPN private DNS opt-in structured.
# 函数用途: 从显式参数或环境配置读取是否允许私网解析，但 metadata/link-local 仍由 gate 永久拦截。
def _effective_allow_private_resolution(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("MY_AGENT_ALLOW_PRIVATE_URLS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# LLM: _format_http_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 format_http_error 转成人或模型可读的展示文本。
def _format_http_error(tool: str, exc: urllib.error.HTTPError, max_chars: int) -> ToolExecutionResult:
    detail = exc.read(max_chars + 1).decode("utf-8", "replace")
    result = (
        f"HTTP {exc.code}\n"
        f"content_type={exc.headers.get('Content-Type', '')}\n\n"
        f"{detail[:max_chars]}"
    )
    if len(detail) > max_chars:
        result += "\n... 已截断"
    return ToolExecutionResult(tool, False, result)


# LLM: _response_preview_chars lets agents request small previews without raising global config.
# 函数用途: 读取可选 max_chars，但只能缩小到本工具配置上限，避免大网页反复灌进模型上下文。
def _response_preview_chars(params: dict[str, Any], configured_max: int) -> int:
    raw = params.get("max_chars")
    if raw in (None, ""):
        return configured_max
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return configured_max
    return max(_MIN_RESPONSE_PREVIEW_CHARS, min(configured_max, requested))


# LLM: WebFetchTool keeps the old import path while implementation lives in web_fetch_tools.py.
# 类用途: 给旧调用方保留 agent.tooling.web.WebFetchTool，同时注入共享 URL、安全和错误格式 helper。
class WebFetchTool(_WebFetchTool):
    # LLM: WebFetchTool.__init__ keeps the compatibility wrapper thin while sharing the new web_fetch runtime.
    # 函数用途: 把旧构造参数转换成 WebRuntimeDeps，不新增第二套网络工具入口。
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
