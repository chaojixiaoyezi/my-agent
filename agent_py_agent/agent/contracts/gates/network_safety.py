
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ...common.value_parsing import text_value as _text
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
_RFC2544_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class NetworkSafetyFacts:
    url: object
    resolver: NetworkResolver
    allowed_private_hosts: Iterable[str] = ()
    previous_resolved_ips: Iterable[str] = ()
    allow_private_resolution: bool = False
    allow_benchmark_resolution: bool = True


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
    return _resolved_host_decision(host, facts, private_host_allowed)


def _resolved_host_decision(host: str, facts: NetworkSafetyFacts, private_host_allowed: bool) -> GateDecision:
    resolved = _resolve_host(facts.resolver, host)
    if isinstance(resolved, GateDecision):
        return resolved

    previous = _canonical_ip_list(facts.previous_resolved_ips)
    code, blocked_ip = _resolved_block(resolved, previous, private_host_allowed, facts)
    if blocked_ip:
        return _deny(code, _resolved_evidence(host, resolved, previous, blocked_ip))
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


__all__ = ["NetworkResolver", "NetworkSafetyFacts", "always_blocked_host", "evaluate_network_safety_gate"]


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
