
from __future__ import annotations

"""Runtime capability resolution for ordinary SimpleAgent turns.

The registry owns final security-tool authorization. This module only decides
whether a normal run/chat turn carries an explicit logs/security protocol
marker. Natural-language security/log phrases are left to the model and tool
catalog, not hard-coded product logic.
"""

import re
from collections.abc import Iterable

from ...log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability

SECURITY_RUNTIME_CAPABILITY = "logs/security"

_EXPLICIT_SECURITY_MARKERS = {
    SECURITY_RUNTIME_CAPABILITY,
    "log_analysis",
    "security_logs",
    *SECURITY_TOOL_NAMES,
}

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
    return _contains_keyword(lowered, _EXPLICIT_SECURITY_MARKERS)


def _contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    for keyword in keywords:
        if _contains_single_keyword(text, keyword):
            return True
    return False


def _contains_single_keyword(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9_ ]+", keyword):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])", text))
    return keyword in text
