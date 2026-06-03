
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model_visible_refs import is_legacy_subagent_path, scrub_legacy_subagent_paths_in_text

_HIDDEN_LEGACY_SUBAGENT_REF = "[internal_legacy_subagent_path_hidden]"
_INTERNAL_MODEL_VISIBLE_TOOLS = frozenset(
    {
        "create_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
        "schedule_child_subagents",
        "subagent_board",
    }
)


def sanitize_model_visible_refs(value: Any) -> Any:
    return _sanitize_model_visible_refs(value, field="")


def _sanitize_model_visible_refs(value: Any, *, field: str) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_model_visible_refs(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_model_visible_refs(item, field=field) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_model_visible_refs(item, field=field) for item in value]
    if isinstance(value, Path):
        return _sanitize_model_visible_refs(str(value), field=field)
    if isinstance(value, str):
        return _sanitized_string(value, field=field)
    return value


def _sanitized_string(value: str, *, field: str) -> str:
    if not is_legacy_subagent_path(value):
        return value
    if field in {"output_files", "output_refs", "artifact_refs"}:
        return _legacy_ref_basename(value)
    if field in {"goal", "summary", "latest_summary", "last_progress_summary", "message", "reason", "note"}:
        return scrub_legacy_subagent_paths_in_text(value)
    return _HIDDEN_LEGACY_SUBAGENT_REF


def _legacy_ref_basename(value: str) -> str:
    name = Path(str(value).replace("\\", "/")).name
    return name or _HIDDEN_LEGACY_SUBAGENT_REF


def sanitize_model_visible_tool_output(tool: str, output: str) -> str:
    if not is_model_visible_internal_tool(tool):
        return output
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        sanitized = sanitize_model_visible_refs(output)
        return sanitized if isinstance(sanitized, str) else output
    return json.dumps(sanitize_model_visible_refs(value), ensure_ascii=False, indent=2)


def is_model_visible_internal_tool(tool: str) -> bool:
    return str(tool or "").strip() in _INTERNAL_MODEL_VISIBLE_TOOLS


__all__ = [
    "is_legacy_subagent_path",
    "is_model_visible_internal_tool",
    "sanitize_model_visible_refs",
    "sanitize_model_visible_tool_output",
]
