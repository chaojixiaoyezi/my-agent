
from __future__ import annotations

from typing import Any

from ...models import SubAgentTask

_PLACEHOLDER_AGENT_NAMES = {
    "",
    "worker",
    "general",
    "subagent",
    "agent",
    "child",
}


def scheduled_child_agent_name(parent: SubAgentTask, spec: Any, *, sibling_index: int = 1) -> str:
    depth = max(1, int(parent.depth or 0) + 1)
    raw_name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    if raw_name and not _is_placeholder_name(raw_name) and not _is_placeholder_suffix(raw_name):
        return raw_name
    suffix = _agent_name_suffix(getattr(spec, "role", "worker"))
    if not _has_trailing_identifier(suffix):
        suffix = f"{suffix}-{max(1, int(sibling_index or 1))}"
    return f"agent-d{depth}-{suffix}"


def _agent_name_suffix(value: str, default: str = "worker") -> str:
    text = str(value or "").strip().replace("_", "-").strip("-") or "worker"
    default_text = str(default or "").strip().replace("_", "-").strip("-") or "worker"
    if _is_placeholder_suffix(text):
        return default_text
    return text


def _is_placeholder_name(value: str) -> bool:
    text = str(value or "").strip().strip("-").casefold()
    return text in _PLACEHOLDER_AGENT_NAMES


def _has_trailing_identifier(value: str) -> bool:
    return str(value or "").strip().rsplit("-", 1)[-1].isdigit()


def _is_placeholder_suffix(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return not text or "*" in text
