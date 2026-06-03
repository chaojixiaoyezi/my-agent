
from __future__ import annotations

from ..subagents.role_templates import COORDINATOR_TOOLS


def explicit_root_allowed_tools(allowed_tools: list[str] | None) -> list[str] | None:
    if allowed_tools is None:
        return None
    return list(dict.fromkeys([*allowed_tools, *COORDINATOR_TOOLS]))
