# LLM: 该模块接线网络抓取共用闸门；运行端口来自 G4 注册表，不能重新引入 Gateway 服务层反向依赖。
# 模块用途: 装配网页读取工具、URL 解析和网络安全回执，DNS/重定向均要复用同一份检查策略。
from __future__ import annotations

"""implements outbound HTTP tools behind explicit timeout and output-size limits.

这个文件只负责访问网络。
`web_fetch` 是唯一的 URL/API 读取入口：单页读取、批量抽取和简单 HTTP 请求都走它。
这里统一限制超时时间和返回长度，避免一次请求把主流程卡死或把 prompt 撑爆。
"""

import errno
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..contracts.gates.network_safety import (
    GatewayEndpointConfig,
    NetworkResolver,
    NetworkSafetyFacts,
    NetworkSafetySettings,
    evaluate_network_safety_gate,
)
from .models import ToolHandlerOutcome
from .web_fetch_tools import WebFetchTool as _WebFetchTool
from .web_fetch_tools import WebRuntimeDeps
from .web_http_helpers import MIN_RESPONSE_PREVIEW_CHARS, scalar_text

# URL 最多 4096 字符：超长拒绝，防异常输入。
_MAX_URL_CHARS = 4096


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


# LLM: 所有抓取入口共用这一结构化闸；实际 Gateway 监听事实每跳刷新，错误码与模型可见提示必须一致。
# 函数用途: 把 network_safety 的拒绝转成工具错误回执，不连接被拒目标。
def _network_safety_error(
    tool: str,
    url: str,
    resolver: NetworkResolver,
    settings: NetworkSafetySettings | None = None,
) -> ToolHandlerOutcome | None:
    settings = settings or NetworkSafetySettings()
    gateway_ports, port_state, port_source = _effective_gateway_endpoint(
        settings.gateway_endpoint
    )
    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            url=url,
            resolver=resolver,
            allowed_private_hosts=settings.allowed_private_hosts,
            allow_private_resolution=_effective_allow_private_resolution(
                settings.allow_private_resolution
            ),
            gateway_ports=gateway_ports,
            gateway_port_state=port_state,
            gateway_port_source=port_source,
            local_address_probe=_probe_local_address,
        )
    )
    if decision.allowed:
        return None
    code = decision.finding_codes[0] if decision.finding_codes else "NETWORK_SAFETY_DENIED"
    return ToolHandlerOutcome(
        tool,
        False,
        f"网络安全检查失败: {code}{_private_host_recovery_note(code, url)}",
        result_envelope={"network_safety_gate": decision.to_dict()},
        error_code=code,
    )


# LLM: 提示只按结构化错误码选择；G6 是不可授权绕过的本机 Gateway 边界，不可误导成普通私网申请。
# 函数用途: 为网络拒绝附上正确恢复方向，私网授权与 Gateway 端口拒绝分开说明。
def _private_host_recovery_note(code: str, url: str) -> str:
    """私网拦截自带可执行的自纠通路(真机回归② N1:子代理撞闸后不知道有授权路,直接放弃拖崩
    编队)。只对私网类拦截追加;metadata/link-local 永久拦截等其余码保持原样(本就无路可走)。"""
    if code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED":
        return "（宿主侧抓取工具不能访问本机 Gateway 端口；请改用 Gateway 之外的公开来源。）"
    if code == "NETWORK_GATEWAY_PORT_UNAVAILABLE":
        return "（无法确认本机 Gateway 实际端口，已按安全策略拒绝本机目标；请勿改用别名或映射地址重试。）"
    if code == "NETWORK_GATEWAY_LOCAL_ADDRESS_UNAVAILABLE":
        return "（无法确认目标是否为本机地址，已按安全策略拒绝该端口；请改用 Gateway 之外的来源。）"
    if code not in ("NETWORK_PRIVATE_HOST_BLOCKED", "NETWORK_PRIVATE_IP_BLOCKED"):
        return ""
    host = urllib.parse.urlsplit(url).hostname or ""
    return (
        f"(目标 {host} 是内网/私网地址,默认出站防护拦截——这是授权缺口,不是网络故障。"
        "若它正是用户任务指定的目标: 主代理→如实向用户说明内网访问缺口(当前底座不提供白名单授权),"
        "或与用户确认后换公网来源;子代理→调 capability_request(capability_type=network, "
        f"network_scope=[\"{host}\"], requested_tools=[\"web_fetch\"]) 申请授权,等父代理处理期间"
        "继续其他可做的工作,不要因此放弃(abandon)整个任务。)"
    )


# LLM: 每跳从 G4 进程注册表取实际绑定端口，并合并正数配置端口；不从 Gateway 模块反向读取服务对象。
# 函数用途: 组合 G4 运行端口和 TUI/跨进程配置端口，说明无端口、端口无效或可检查状态。
def _effective_gateway_endpoint(
    config: GatewayEndpointConfig,
) -> tuple[tuple[int, ...], str, str]:
    from ..attempt.sandbox import gateway_bound_ports

    try:
        registered = tuple(gateway_bound_ports())
    except Exception:
        registered = ()
        registry_readable = False
    else:
        registry_readable = True
    configured = _valid_gateway_port(config.configured_port)
    ports = {_valid_gateway_port(port) for port in registered}
    ports.discard(None)
    if configured is not None:
        ports.add(configured)
    if ports:
        sources = []
        if any(_valid_gateway_port(port) is not None for port in registered):
            sources.append("registry")
        if configured is not None:
            sources.append("config")
        return tuple(sorted(ports)), "known", "+".join(sources)
    if registry_readable and _configured_port_missing_or_disabled(config.configured_port):
        return (), "not_applicable", "none"
    return (), "unavailable", "unavailable"


# LLM: 只有 Gateway 端口命中后才做 bind 探测；不联网、不枚举主机名地址，也不缓存结果。
# 函数用途: 用 UDP bind 判断一个解析 IP 是否配置在本机；EADDRNOTAVAIL 是明确的非本机，其它错误保留未知。
def _probe_local_address(value: str) -> bool | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = None
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.bind((str(address), 0))
        return True
    except OSError as exc:
        return False if exc.errno == errno.EADDRNOTAVAIL else None
    finally:
        if sock is not None:
            sock.close()


# LLM: 只有整数 0 表示明确关闭 HTTP；None 表示无配置，非法值不能伪装成关闭或有效端口。
# 函数用途: 判断配置端口是否确实声明“不启用 HTTP 服务”。
def _configured_port_missing_or_disabled(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value == 0)


# LLM: 端口仅接受合法 TCP 整数；不强转字符串、浮点或布尔，避免把坏配置静默变成另一个端口。
# 函数用途: 校验 Gateway 配置端口后备。
def _valid_gateway_port(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 1 <= value <= 65535 else None

def _effective_allow_private_resolution(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("MY_AGENT_ALLOW_PRIVATE_URLS", "")
    return raw.strip().lower() in {"1", "true"}


def _format_http_error(tool: str, exc: urllib.error.HTTPError, max_chars: int) -> ToolHandlerOutcome:
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
    return ToolHandlerOutcome(tool, False, result, error_code=code)


def _response_preview_chars(params: dict[str, Any], configured_max: int) -> int:
    raw = params.get("max_chars")
    if raw in (None, ""):
        return configured_max
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return configured_max
    return max(MIN_RESPONSE_PREVIEW_CHARS, min(configured_max, requested))


# LLM: 保持旧工具构造入口，把稳定依赖和端口配置后备交给核心实现；授权仍由每次调用快照提供。
# 类用途: 为 registry 装配统一网页抓取实现，修改此包装时要同步 registry 与直接构造测试。
class WebFetchTool(_WebFetchTool):
    # LLM: 构造时只冻结 TUI Gateway 配置后备；运行时实际绑定端口由网络回调动态读取。
    # 函数用途: 将抓取参数与 Gateway 端点后备交给统一 web_fetch 实现。
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
        gateway_endpoint_config: GatewayEndpointConfig | None = None,
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
            gateway_endpoint_config=(
                gateway_endpoint_config
                if gateway_endpoint_config is not None
                else GatewayEndpointConfig()
            ),
        ))
