# LLM: 所有宿主 HTTP 出站目标都经此结构化闸；G6 仅依靠端口、本机探针事实，不能与私网授权合并。
# 模块用途: 集中判定 DNS、私网及本机 Gateway 端口边界，任何放行调整都要同步覆盖 web_fetch/watch_stream。
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from ...common.value_parsing import text_value as _text
from .models import GateDecision, GateFinding

NetworkResolver = Callable[[str], Iterable[object]]
LocalAddressProbe = Callable[[str], bool | None]
GatewayPortState = Literal["known", "not_applicable", "unavailable"]

_GATE = "network_safety"
_PRIVATE_HOSTS = {"localhost"}
_ALWAYS_BLOCKED_HOSTS = {"metadata.google.internal", "metadata.goog"}
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
)
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_RFC2544_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


# LLM: 配置端口只作进程内 G4 注册表的补充事实；不要把进程状态或本机地址发现藏进这个配置对象。
# 类用途: 传递 TUI/跨进程部署使用的 Gateway 配置端口，运行端口由调用层动态读取。
@dataclass(frozen=True)
class GatewayEndpointConfig:
    configured_port: object = None


# LLM: 私网授权与 Gateway 端点来源是两条独立事实；不要把允许访问私网解释成可访问本机 Gateway。
# 类用途: 将网络工具的 allowlist、开关和 Gateway 配置作为一次请求的不可变策略输入。
@dataclass(frozen=True)
class NetworkSafetySettings:
    allowed_private_hosts: Iterable[str] = ()
    allow_private_resolution: bool | None = None
    gateway_endpoint: GatewayEndpointConfig = GatewayEndpointConfig()


# LLM: G6 仅用结构化端口状态和逐目标本机探针；N/A 不查，未知端口只拒已确认本机地址。
# 类用途: 汇总网络请求、DNS 回答、受保护端口和本机探测回调，供纯安全闸判断。
@dataclass(frozen=True)
class NetworkSafetyFacts:
    url: object
    resolver: NetworkResolver
    allowed_private_hosts: Iterable[str] = ()
    previous_resolved_ips: Iterable[str] = ()
    allow_private_resolution: bool = False
    allow_benchmark_resolution: bool = True
    gateway_ports: Iterable[int] = ()
    gateway_port_state: GatewayPortState = "not_applicable"
    gateway_port_source: str = "none"
    local_address_probe: LocalAddressProbe | None = None


# LLM: 唯一网络闸入口；G6 必须在普通私网放行后仍核对解析地址、请求端口和结构化 Gateway 事实。
# 函数用途: 统一决定 URL 是否可访问，新增地址规则须同时覆盖字面 IP 与解析后的 DNS 地址。
def evaluate_network_safety_gate(facts: NetworkSafetyFacts) -> GateDecision:
    parsed = urlparse(_text(facts.url))
    scheme = _text(parsed.scheme).lower()
    if scheme == "file":
        return _deny("NETWORK_FILE_URL_BLOCKED", {"scheme": scheme})

    host = _normalize_host(parsed.hostname)
    if not host:
        return _deny("NETWORK_HOST_REQUIRED", {"scheme": scheme})
    if host in _ALWAYS_BLOCKED_HOSTS:
        return _deny("NETWORK_ALWAYS_BLOCKED_HOST", {"host": host})

    allowed_private_hosts = _normalized_allowlist(facts.allowed_private_hosts)
    private_host_allowed = _host_is_allowlisted(host, allowed_private_hosts)
    literal_ip = _canonical_ip(host)
    if literal_ip:
        return _literal_host_decision(host, literal_ip, facts, private_host_allowed)

    if _is_private_hostname(host) and not private_host_allowed and not facts.allow_private_resolution:
        return _deny("NETWORK_PRIVATE_HOST_BLOCKED", {"host": host})
    return _resolved_host_decision(host, facts, private_host_allowed)


# LLM: 域名 DNS 结果会被 pin；永久拦截与旧私网策略先保留，之后叠加独立 Gateway 端口检查。
# 函数用途: 校验一个域名解析结果，拒绝重绑定和本机 Gateway 端口目标。
def _resolved_host_decision(host: str, facts: NetworkSafetyFacts, private_host_allowed: bool) -> GateDecision:
    resolved = _resolve_host(facts.resolver, host)
    if isinstance(resolved, GateDecision):
        return resolved

    previous = _canonical_ip_list(facts.previous_resolved_ips)
    code, blocked_ip = _resolved_block(resolved, previous, private_host_allowed, facts)
    if blocked_ip:
        return _deny(code, _resolved_evidence(host, resolved, previous, blocked_ip))
    gateway_denial = _gateway_port_denial(facts, resolved)
    if gateway_denial is not None:
        return gateway_denial
    return _allow_resolved_host(host, resolved, previous, facts)


def _resolved_block(
    resolved: tuple[str, ...],
    previous: tuple[str, ...],
    private_host_allowed: bool,
    facts: NetworkSafetyFacts,
) -> tuple[str, str]:
    always_blocked_ip = _first_always_blocked_ip(resolved)
    if always_blocked_ip:
        return "NETWORK_ALWAYS_BLOCKED_IP", always_blocked_ip
    blocked_ip = _first_blocked_ip(
        resolved,
        private_host_allowed,
        facts.allow_private_resolution,
        facts.allow_benchmark_resolution,
    )
    code = "NETWORK_DNS_REBINDING_BLOCKED" if previous else "NETWORK_PRIVATE_IP_BLOCKED"
    return (code, blocked_ip) if blocked_ip else ("", "")


def _allow_resolved_host(
    host: str,
    resolved: tuple[str, ...],
    previous: tuple[str, ...],
    facts: NetworkSafetyFacts,
) -> GateDecision:
    return GateDecision.allow(
        _GATE,
        evidence={
            "host": host,
            "resolved_ips": list(resolved),
            "previous_resolved_ips": list(previous),
            "dns_rechecked": bool(previous),
            "private_host_allowed": _host_is_allowlisted(host, _normalized_allowlist(facts.allowed_private_hosts)),
            "allow_private_resolution": bool(facts.allow_private_resolution),
            "allow_benchmark_resolution": bool(facts.allow_benchmark_resolution),
            "benchmark_resolution_allowed_ips": _benchmark_ips(resolved, facts.allow_benchmark_resolution),
        },
    )


# LLM: 字面 IP 仍先保留永久地址和普通私网拒绝语义；仅在旧规则放行后叠加独立 Gateway 端口判断。
# 函数用途: 检查 URL 直接写出的 IP，并让私网授权不能绕过本机 Gateway 端口保护。
def _literal_host_decision(
    host: str,
    ip: str,
    facts: NetworkSafetyFacts,
    private_host_allowed: bool,
) -> GateDecision:
    if _is_always_blocked_ip(ip):
        return _deny("NETWORK_ALWAYS_BLOCKED_IP", {"host": host, "ip": ip})
    if _is_private_ip(ip) and not private_host_allowed and not facts.allow_private_resolution:
        return _deny("NETWORK_PRIVATE_HOST_BLOCKED", {"host": host, "ip": ip})
    gateway_denial = _gateway_port_denial(facts, (ip,))
    if gateway_denial is not None:
        return gateway_denial
    return GateDecision.allow(
        _GATE,
        evidence={
            "host": host,
            "resolved_ips": [ip],
            "previous_resolved_ips": [],
            "dns_rechecked": False,
            "private_host_allowed": private_host_allowed,
            "allow_private_resolution": bool(facts.allow_private_resolution),
        },
    )


# LLM: 只在已知 Gateway 端口命中时探测本机；未知端口时只挡确认的本机目标，N/A 完全跳过。
# 函数用途: 把受保护端口、本机探针结果和解析后的 IP 组合为 G6 的结构化拒绝。
def _gateway_port_denial(
    facts: NetworkSafetyFacts,
    resolved: Iterable[str],
) -> GateDecision | None:
    endpoint_port = _url_port(facts.url)
    gateway_ports = _valid_gateway_ports(facts.gateway_ports)
    evidence = {
        "target_port": endpoint_port,
        "gateway_ports": list(gateway_ports),
        "gateway_port_state": facts.gateway_port_state,
        "gateway_port_source": facts.gateway_port_source,
        "resolved_ips": list(resolved),
    }
    port_unknown = not gateway_ports and facts.gateway_port_state == "unavailable"
    if facts.gateway_port_state == "not_applicable" and not gateway_ports:
        return None
    if not port_unknown and endpoint_port not in gateway_ports:
        return None
    for ip in resolved:
        local = _local_address_status(ip, facts.local_address_probe)
        if local is True:
            code = "NETWORK_GATEWAY_PORT_UNAVAILABLE" if port_unknown else "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
            return _deny(code, {**evidence, "ip": ip})
        if local is None:
            return _deny("NETWORK_GATEWAY_LOCAL_ADDRESS_UNAVAILABLE", evidence)
    return None


# LLM: 端口注册表和配置只收正整数 TCP 端口；非法值不能变成误匹配或字符串比较。
# 函数用途: 清理受保护端口集合，消除重复项并排除布尔、越界和非整数值。
def _valid_gateway_ports(values: Iterable[object]) -> tuple[int, ...]:
    ports = {
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
    }
    return tuple(sorted(ports))


# LLM: 默认 HTTP/HTTPS 端口和显式 URL 端口走同一结构化值，解析异常不能转成字符串比较。
# 函数用途: 取出 URL 实际连接端口，供每一跳的 Gateway 目标判断复用。
def _url_port(url: object) -> int | None:
    parsed = urlparse(_text(url))
    if parsed.port is not None:
        return parsed.port
    return {"http": 80, "https": 443}.get(_text(parsed.scheme).lower())


# LLM: 回环/未指定地址由 IP 属性直接识别；其他地址必须通过调用方逐目标 bind 事实分类。
# 函数用途: 返回本机、非本机或无法确认，调用异常不能被默认为公网。
def _local_address_status(value: str, probe: LocalAddressProbe | None) -> bool | None:
    address = ip_address_or_none(value)
    if address is None:
        return None
    if address.is_loopback or address.is_unspecified:
        return True
    projected = ipv4_projected(address) or address
    if projected.is_loopback or projected.is_unspecified:
        return True
    if probe is None:
        return None
    try:
        result = probe(str(address))
    except Exception:
        return None
    return result if isinstance(result, bool) else None


def _resolve_host(resolver: NetworkResolver, host: str) -> tuple[str, ...] | GateDecision:
    try:
        answers = tuple(_address_text(item) for item in resolver(host))
    except Exception as exc:  # pragma: no cover - defensive boundary normalization
        return _deny("NETWORK_HOST_RESOLUTION_FAILED", {"host": host, "error_type": type(exc).__name__})

    resolved_entries = tuple(_canonical_ip(answer) for answer in answers)
    if any(not entry for entry in resolved_entries):
        return _deny("NETWORK_RESOLVED_IP_INVALID", {"host": host, "answers": list(answers)})
    resolved = _dedupe_ips(resolved_entries)
    if not resolved:
        return _deny("NETWORK_HOST_RESOLUTION_FAILED", {"host": host})
    return resolved


def _first_always_blocked_ip(resolved: Iterable[str]) -> str:
    return next((ip for ip in resolved if _is_always_blocked_ip(ip)), "")


def _first_blocked_ip(
    resolved: Iterable[str],
    private_host_allowed: bool,
    allow_private_resolution: bool,
    allow_benchmark_resolution: bool,
) -> str:
    if private_host_allowed or allow_private_resolution:
        return ""
    return next((ip for ip in resolved if _is_private_ip(ip) and not _benchmark_allowed(ip, allow_benchmark_resolution)), "")


def _resolved_evidence(host: str, resolved: Iterable[str], previous: Iterable[str], blocked_ip: str) -> dict[str, Any]:
    return {
        "host": host,
        "ip": blocked_ip,
        "resolved_ips": list(resolved),
        "previous_resolved_ips": list(previous),
    }


def _canonical_ip_list(values: Iterable[object]) -> tuple[str, ...]:
    return _dedupe_ips(_canonical_ip(_address_text(value)) for value in values)


def _dedupe_ips(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for ip in values:
        if not ip or ip in result:
            continue
        result.append(ip)
    return tuple(result)


def _benchmark_ips(resolved: Iterable[str], allow_benchmark_resolution: bool) -> list[str]:
    return [ip for ip in resolved if allow_benchmark_resolution and _is_benchmark_proxy_ip(ip)]


def _benchmark_allowed(ip: str, allow_benchmark_resolution: bool) -> bool:
    return bool(allow_benchmark_resolution and _is_benchmark_proxy_ip(ip))


def _canonical_ip(host: object) -> str:
    normalized = _normalize_host(host)
    if not normalized:
        return ""
    numeric = _numeric_ipv4_host(normalized)
    if numeric:
        normalized = numeric
    shorthand = _shorthand_ipv4_host(normalized)
    if shorthand:
        normalized = shorthand
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return ""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


def _is_benchmark_proxy_ip(value: str) -> bool:
    address = ip_address_or_none(value)
    projected = ipv4_projected(address) if address is not None else None
    return isinstance(projected, ipaddress.IPv4Address) and projected in _RFC2544_BENCHMARK_NETWORK


def _is_private_ip(value: str) -> bool:
    address = ip_address_or_none(value)
    if address is None:
        return True
    address = ipv4_projected(address) or address
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_NETWORK)
    )


def _is_always_blocked_ip(value: str) -> bool:
    address = ip_address_or_none(value)
    if address is None:
        return True
    address = ipv4_projected(address) or address
    return any(address in network for network in _ALWAYS_BLOCKED_NETWORKS)


def _is_private_hostname(host: str) -> bool:
    return _normalize_host(host) in _PRIVATE_HOSTS


def _normalized_allowlist(values: Iterable[str]) -> set[str]:
    return {_normalize_host(value) for value in values if _normalize_host(value)}


def _host_is_allowlisted(host: str, allowlist: set[str]) -> bool:
    if host in allowlist:
        return True
    literal = _canonical_ip(host)
    return bool(literal and literal in allowlist)


def _numeric_ipv4_host(host: str) -> str:
    if not host.isdigit():
        return ""
    try:
        value = int(host)
    except ValueError:
        return ""
    if not 0 <= value <= 0xFFFFFFFF:
        return ""
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _shorthand_ipv4_host(host: str) -> str:
    parts = host.split(".")
    if not 1 < len(parts) < 4 or not all(part.isdigit() for part in parts):
        return ""
    nums = [int(part) for part in parts]
    if any(num < 0 or num > 255 for num in nums):
        return ""
    if len(nums) == 2:
        nums = [nums[0], 0, 0, nums[1]]
    elif len(nums) == 3:
        nums = [nums[0], nums[1], 0, nums[2]]
    return ".".join(str(num) for num in nums)


def _address_text(value: object) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("address"))
    address = getattr(value, "address", None)
    return _text(address if address is not None else value)

def _normalize_host(value: object) -> str:
    return _text(value).lower().strip("[]").rstrip(".")


def _deny(code: str, evidence: dict[str, Any]) -> GateDecision:
    return GateDecision(
        _GATE,
        "DENY",
        False,
        (GateFinding(code, evidence=evidence),),
        "repair_network_request",
        {},
    )


def always_blocked_host(host: object) -> bool:
    """该主机是否属于「永久拦截」段(云 metadata / link-local 凭证端点)。
    这类端点即使进入 allowed_private_hosts 也不放行——写进闸内也无效,
    早拒绝比落一条永远无效的授权诚实。"""
    normalized = _normalize_host(host)
    if normalized in _ALWAYS_BLOCKED_HOSTS:
        return True
    ip = _canonical_ip(normalized)
    return bool(ip) and _is_always_blocked_ip(ip)


__all__ = [
    "GatewayEndpointConfig",
    "NetworkResolver",
    "NetworkSafetyFacts",
    "NetworkSafetySettings",
    "always_blocked_host",
    "evaluate_network_safety_gate",
]


# ---- 原 network/address_projection.py 并入 ----
import ipaddress


def ip_address_or_none(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def ipv4_projected(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if isinstance(address, ipaddress.IPv4Address):
        return address
    return address.ipv4_mapped or embedded_ipv4_from_ipv6(address)


def embedded_ipv4_from_ipv6(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    parts = tuple(int(part, 16) for part in address.exploded.split(":"))
    return _matching_ipv4_projection(_ipv4_projection_candidates(parts))


def _ipv4_projection_candidates(parts: tuple[int, ...]) -> tuple[tuple[bool, int, int], ...]:
    return (
        (parts[:6] == (0, 0, 0, 0, 0, 0), parts[6], parts[7]),
        (parts[:6] == (0, 0, 0, 0, 0xFFFF, 0), parts[6], parts[7]),
        (parts[:3] == (0x0064, 0xFF9B, 0x0001) and parts[3:6] == (0, 0, 0), parts[6], parts[7]),
        (parts[0] == 0x2002, parts[1], parts[2]),
        (parts[0] == 0x2001 and parts[1] == 0, parts[6] ^ 0xFFFF, parts[7] ^ 0xFFFF),
        ((parts[4] & 0xFCFF) == 0 and parts[5] == 0x5EFE, parts[6], parts[7]),
    )


def _matching_ipv4_projection(candidates: tuple[tuple[bool, int, int], ...]) -> ipaddress.IPv4Address | None:
    for matches, high, low in candidates:
        if matches:
            return ipaddress.IPv4Address(((high & 0xFFFF) << 16) | (low & 0xFFFF))
    return None
