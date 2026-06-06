
from __future__ import annotations

"""Runtime capability resolution for ordinary SimpleAgent turns.

The registry owns final security-tool authorization. This module only accepts
structured grants/injections; user prompt prose never grants runtime capability.
"""

from collections.abc import Iterable

from ...log_analysis.capabilities import has_security_tool_capability

SECURITY_RUNTIME_CAPABILITY = "logs/security"

def resolve_runtime_capabilities(
    user_prompt: str,
    *,
    inject: Iterable[str] | None = None,
    granted_capabilities: Iterable[str] | None = None,
) -> list[str]:
    capabilities = _normalize_capabilities(granted_capabilities)
    if has_security_tool_capability(capabilities):
        return capabilities

    _ = user_prompt
    if has_security_tool_capability(_normalize_capabilities(inject)):
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
