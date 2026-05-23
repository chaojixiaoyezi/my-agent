# LLM: Network address projection helpers normalize IPv6 wrapper forms before safety gates decide.
# 模块用途: 将 IPv4-mapped、6to4、Teredo、ISATAP 等地址形式投影成稳定机器字段，不做网络 I/O。

from __future__ import annotations

import ipaddress


# LLM: ip_address_or_none normalizes invalid address parsing into a nullable value.
# 函数用途: 防止调用点重复 try/except，并保持非法地址的 deny/false 语义由上层决定。
def ip_address_or_none(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


# LLM: ipv4_projected maps IPv6 wrapper forms to their embedded IPv4 address.
# 函数用途: 让私网、metadata 和 benchmark 检查共用同一 IPv4 投影规则。
def ipv4_projected(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if isinstance(address, ipaddress.IPv4Address):
        return address
    return address.ipv4_mapped or embedded_ipv4_from_ipv6(address)


# LLM: embedded_ipv4_from_ipv6 extracts embedded IPv4 forms used for SSRF bypasses.
# 函数用途: 从 IPv4-compatible、6to4、Teredo、ISATAP 等 IPv6 形式还原 IPv4 地址。
def embedded_ipv4_from_ipv6(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    parts = tuple(int(part, 16) for part in address.exploded.split(":"))
    return _matching_ipv4_projection(_ipv4_projection_candidates(parts))


# LLM: _ipv4_projection_candidates lists structured IPv6 embedding patterns.
# 函数用途: 用布尔条件和高低 16 位字段描述可还原 IPv4 的候选形式。
def _ipv4_projection_candidates(parts: tuple[int, ...]) -> tuple[tuple[bool, int, int], ...]:
    return (
        (parts[:6] == (0, 0, 0, 0, 0, 0), parts[6], parts[7]),
        (parts[:6] == (0, 0, 0, 0, 0xFFFF, 0), parts[6], parts[7]),
        (parts[:3] == (0x0064, 0xFF9B, 0x0001) and parts[3:6] == (0, 0, 0), parts[6], parts[7]),
        (parts[0] == 0x2002, parts[1], parts[2]),
        (parts[0] == 0x2001 and parts[1] == 0, parts[6] ^ 0xFFFF, parts[7] ^ 0xFFFF),
        ((parts[4] & 0xFCFF) == 0 and parts[5] == 0x5EFE, parts[6], parts[7]),
    )


# LLM: _matching_ipv4_projection returns the first candidate whose machine predicate matched.
# 函数用途: 将匹配的高低 16 位组合成 IPv4Address，没匹配则返回 None。
def _matching_ipv4_projection(candidates: tuple[tuple[bool, int, int], ...]) -> ipaddress.IPv4Address | None:
    for matches, high, low in candidates:
        if matches:
            return ipaddress.IPv4Address(((high & 0xFFFF) << 16) | (low & 0xFFFF))
    return None


__all__ = ["embedded_ipv4_from_ipv6", "ip_address_or_none", "ipv4_projected"]
