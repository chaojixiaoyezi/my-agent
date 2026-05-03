from __future__ import annotations

"""Runtime capability inference for ordinary SimpleAgent turns.

The registry owns final security-tool authorization. This module only decides
whether a normal run/chat turn carries an explicit or obvious logs/security
intent.
"""

import re
from collections.abc import Iterable

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability

SECURITY_RUNTIME_CAPABILITY = "logs/security"

_EXPLICIT_SECURITY_MARKERS = {
    SECURITY_RUNTIME_CAPABILITY,
    "log_analysis",
    "security_logs",
    *SECURITY_TOOL_NAMES,
}

_SECURITY_LOG_PHRASES = (
    "security log",
    "security logs",
    "security event",
    "security events",
    "security telemetry",
    "audit log",
    "audit logs",
    "firewall log",
    "firewall logs",
    "waf log",
    "waf logs",
    "auth log",
    "auth logs",
    "authentication log",
    "authentication logs",
    "siem",
    "edr log",
    "edr logs",
    "ids log",
    "ids logs",
)

_CJK_SECURITY_LOG_PHRASES = (
    "\u5b89\u5168\u65e5\u5fd7",
    "\u5b89\u5168\u4e8b\u4ef6",
    "\u5ba1\u8ba1\u65e5\u5fd7",
    "\u544a\u8b66\u65e5\u5fd7",
    "\u9632\u706b\u5899\u65e5\u5fd7",
    "\u8ba4\u8bc1\u65e5\u5fd7",
    "\u767b\u5f55\u65e5\u5fd7",
    "\u5a01\u80c1\u65e5\u5fd7",
    "\u5165\u4fb5\u65e5\u5fd7",
    "\u653b\u51fb\u65e5\u5fd7",
    "\u65e5\u5fd7\u6eaf\u6e90",
    "\u65e5\u5fd7\u53d6\u8bc1",
    "waf\u65e5\u5fd7",
)

_LOG_TOKENS = (
    "log",
    "logs",
    "\u65e5\u5fd7",
    "\u544a\u8b66",
    "\u5ba1\u8ba1",
)

_SECURITY_TOKENS = (
    "security",
    "attacker",
    "attack",
    "malicious",
    "suspicious",
    "incident",
    "intrusion",
    "breach",
    "threat",
    "ioc",
    "waf",
    "siem",
    "firewall",
    "ids",
    "ips",
    "edr",
    "alert",
    "exploit",
    "vulnerability",
    "compromise",
    "forensic",
    "hunt",
    "phishing",
    "brute force",
    "bruteforce",
    "cve",
    "\u5b89\u5168",
    "\u653b\u51fb",
    "\u6076\u610f",
    "\u53ef\u7591",
    "\u5165\u4fb5",
    "\u5a01\u80c1",
    "\u6eaf\u6e90",
    "\u53d6\u8bc1",
    "\u5931\u9677",
    "\u6f0f\u6d1e",
    "\u9632\u706b\u5899",
    "\u66b4\u529b\u7834\u89e3",
    "\u9493\u9c7c",
    "\u53d7\u5bb3\u8005",
    "\u653b\u51fb\u8005",
    "\u5c01\u7981",
    "\u62e6\u622a",
)


def resolve_runtime_capabilities(
    user_prompt: str,
    *,
    inject: Iterable[str] | None = None,
    granted_capabilities: Iterable[str] | None = None,
) -> list[str]:
    capabilities = _normalize_capabilities(granted_capabilities)
    if has_security_tool_capability(capabilities):
        return capabilities

    text = "\n".join([user_prompt or "", *(str(item) for item in (inject or []))])
    if _looks_like_security_log_task(text):
        capabilities.append(SECURITY_RUNTIME_CAPABILITY)
    return capabilities


def _normalize_capabilities(capabilities: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in capabilities or []:
        item = str(raw).strip()
        key = item.lower()
        if item and key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized


def _looks_like_security_log_task(text: str) -> bool:
    if not text.strip():
        return False
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)

    if any(marker in lowered for marker in _EXPLICIT_SECURITY_MARKERS):
        return True
    if any(phrase in lowered for phrase in _SECURITY_LOG_PHRASES):
        return True
    if any(phrase in compact for phrase in _CJK_SECURITY_LOG_PHRASES):
        return True

    has_log_signal = _contains_keyword(lowered, _LOG_TOKENS)
    has_security_signal = _contains_keyword(lowered, _SECURITY_TOKENS)
    return has_log_signal and has_security_signal


def _contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    for keyword in keywords:
        if _contains_single_keyword(text, keyword):
            return True
    return False


def _contains_single_keyword(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9_ ]+", keyword):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])", text))
    return keyword in text
