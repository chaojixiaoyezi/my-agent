# LLM: Network safety gates validate adapter URL/DNS boundaries without performing network I/O.
# 模块用途: 用调用方注入的 DNS 事实检查 URL、私网地址和 rebinding，不在 gate 内发起真实网络请求。

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .models import GateDecision, GateFinding

NetworkResolver = Callable[[str], Iterable[object]]

_GATE = "network_safety"
_PRIVATE_HOSTS = {"localhost"}
_ALWAYS_BLOCKED_HOSTS = {"metadata.google.internal", "metadata.goog"}
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
)
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


# LLM: NetworkSafetyFacts keeps this contract helper structure-first and stable.
# 类用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
@dataclass(frozen=True)
class NetworkSafetyFacts:
    url: object
    resolver: NetworkResolver
    allowed_private_hosts: Iterable[str] = ()
    previous_resolved_ips: Iterable[str] = ()
    allow_private_resolution: bool = False


# LLM: evaluate_network_safety_gate blocks private DNS/IP targets before and after adapter requests.
# LLM: evaluate_network_safety_gate keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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
        return _literal_host_decision(host, literal_ip, private_host_allowed, facts.allow_private_resolution)

    if _is_private_hostname(host) and not private_host_allowed and not facts.allow_private_resolution:
        return _deny("NETWORK_PRIVATE_HOST_BLOCKED", {"host": host})

    resolved = _resolve_host(facts.resolver, host)
    if isinstance(resolved, GateDecision):
        return resolved

    previous = _canonical_ip_list(facts.previous_resolved_ips)
    always_blocked_ip = _first_always_blocked_ip(resolved)
    if always_blocked_ip:
        return _deny("NETWORK_ALWAYS_BLOCKED_IP", _resolved_evidence(host, resolved, previous, always_blocked_ip))
    blocked_ip = _first_blocked_ip(resolved, private_host_allowed, facts.allow_private_resolution)
    if blocked_ip:
        code = "NETWORK_DNS_REBINDING_BLOCKED" if previous else "NETWORK_PRIVATE_IP_BLOCKED"
        return _deny(code, _resolved_evidence(host, resolved, previous, blocked_ip))

    return GateDecision.allow(
        _GATE,
        evidence={
            "host": host,
            "resolved_ips": list(resolved),
            "previous_resolved_ips": list(previous),
            "dns_rechecked": bool(previous),
            "private_host_allowed": private_host_allowed,
            "allow_private_resolution": bool(facts.allow_private_resolution),
        },
    )


# LLM: _literal_host_decision keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _literal_host_decision(host: str, ip: str, private_host_allowed: bool, allow_private_resolution: bool) -> GateDecision:
    if _is_always_blocked_ip(ip):
        return _deny("NETWORK_ALWAYS_BLOCKED_IP", {"host": host, "ip": ip})
    if _is_private_ip(ip) and not private_host_allowed and not allow_private_resolution:
        return _deny("NETWORK_PRIVATE_HOST_BLOCKED", {"host": host, "ip": ip})
    return GateDecision.allow(
        _GATE,
        evidence={
            "host": host,
            "resolved_ips": [ip],
            "previous_resolved_ips": [],
            "dns_rechecked": False,
            "private_host_allowed": private_host_allowed,
            "allow_private_resolution": bool(allow_private_resolution),
        },
    )


# LLM: _resolve_host keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _first_blocked_ip keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _first_always_blocked_ip(resolved: Iterable[str]) -> str:
    return next((ip for ip in resolved if _is_always_blocked_ip(ip)), "")


# LLM: _first_blocked_ip keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _first_blocked_ip(resolved: Iterable[str], private_host_allowed: bool, allow_private_resolution: bool) -> str:
    if private_host_allowed or allow_private_resolution:
        return ""
    return next((ip for ip in resolved if _is_private_ip(ip)), "")


# LLM: _resolved_evidence keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _resolved_evidence(host: str, resolved: Iterable[str], previous: Iterable[str], blocked_ip: str) -> dict[str, Any]:
    return {
        "host": host,
        "ip": blocked_ip,
        "resolved_ips": list(resolved),
        "previous_resolved_ips": list(previous),
    }


# LLM: _canonical_ip_list keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _canonical_ip_list(values: Iterable[object]) -> tuple[str, ...]:
    return _dedupe_ips(_canonical_ip(_address_text(value)) for value in values)


# LLM: _dedupe_ips keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _dedupe_ips(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for ip in values:
        if not ip or ip in result:
            continue
        result.append(ip)
    return tuple(result)


# LLM: _canonical_ip keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _is_private_ip keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _is_private_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or address in _CGNAT_NETWORK
    )


# LLM: _is_always_blocked_ip keeps cloud metadata floors non-negotiable.
# 函数用途: 即使开启私网解析授权，也禁止 metadata/link-local 这类凭证端点。
def _is_always_blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in _ALWAYS_BLOCKED_NETWORKS)


# LLM: _is_private_hostname keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _is_private_hostname(host: str) -> bool:
    return _normalize_host(host) in _PRIVATE_HOSTS


# LLM: _normalized_allowlist keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _normalized_allowlist(values: Iterable[str]) -> set[str]:
    return {_normalize_host(value) for value in values if _normalize_host(value)}


# LLM: _host_is_allowlisted keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _host_is_allowlisted(host: str, allowlist: set[str]) -> bool:
    if host in allowlist:
        return True
    literal = _canonical_ip(host)
    return bool(literal and literal in allowlist)


# LLM: _numeric_ipv4_host keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _shorthand_ipv4_host keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _address_text keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _address_text(value: object) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("address"))
    address = getattr(value, "address", None)
    return _text(address if address is not None else value)


# LLM: _text keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _text(value: object) -> str:
    return str(value or "").strip()


# LLM: _normalize_host keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _normalize_host(value: object) -> str:
    return _text(value).lower().strip("[]").rstrip(".")


# LLM: _deny keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _deny(code: str, evidence: dict[str, Any]) -> GateDecision:
    return GateDecision(
        _GATE,
        "DENY",
        False,
        (GateFinding(code, evidence=evidence),),
        "repair_network_request",
        {},
    )


__all__ = ["NetworkResolver", "NetworkSafetyFacts", "evaluate_network_safety_gate"]
