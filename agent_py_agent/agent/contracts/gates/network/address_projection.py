
from __future__ import annotations

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


__all__ = ["embedded_ipv4_from_ipv6", "ip_address_or_none", "ipv4_projected"]
