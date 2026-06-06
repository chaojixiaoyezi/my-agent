
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def structured_read_summary(target: Path, content: str, params: dict[str, Any]) -> str:
    if target.name != "latest_continue_packet.json" or _has_explicit_line_range(params):
        return ""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict) or payload.get("kind") != "subagent_task_local_continue_packet":
        return ""
    return _subagent_continue_packet_summary(payload, target)


def _has_explicit_line_range(params: dict[str, Any]) -> bool:
    return params.get("start_line") is not None or params.get("end_line") is not None


def _subagent_continue_packet_summary(payload: dict[str, Any], target: Path) -> str:
    lines = [
        "structured_read_summary=true",
        "structured_file=latest_continue_packet.json",
        f"source_path={target}",
    ]
    for key in _PACKET_HEADER_KEYS:
        value = payload.get(key)
        if str(value or "").strip():
            lines.append(f"{key}={_inline_summary(value)}")
    lines.extend(_subagent_packet_progress_lines(payload))
    lines.extend(_subagent_packet_session_lines(payload))
    refs = payload.get("recommended_read_paths")
    if isinstance(refs, list) and refs:
        lines.append("recommended_read_paths:")
        lines.extend(f"- {_inline_summary(item)}" for item in refs[:5])
    lines.append(
        "read_policy=This packet body was summarized to save tokens; "
        "if work_progress and session_compact are empty, start from the task goal instead of rereading the packet."
    )
    return "\n".join(lines)


_PACKET_HEADER_KEYS = (
    "schema_version",
    "status",
    "ready_to_continue",
    "continue_mode",
    "current_step",
    "latest_summary",
    "next_action",
)


def _subagent_packet_progress_lines(payload: dict[str, Any]) -> list[str]:
    progress = payload.get("work_progress")
    if not isinstance(progress, dict) or not progress:
        return ["work_progress=none"]
    lines = ["work_progress:"]
    for key in ("summary", "latest_written_path", "next_action", "latest_tool_progress_ref"):
        value = progress.get(key)
        if str(value or "").strip():
            lines.append(f"- {key}: {_inline_summary(value)}")
    headings = progress.get("headings")
    if isinstance(headings, list) and headings:
        lines.append("- headings:")
        lines.extend(f"  - {_inline_summary(item)}" for item in headings[:8])
    return lines


def _subagent_packet_session_lines(payload: dict[str, Any]) -> list[str]:
    session = payload.get("session_compact")
    if not isinstance(session, dict) or not session:
        return ["session_compact=none"]
    lines = ["session_compact:"]
    for key in ("package_id", "metadata_ref", "summary_ref", "next_action"):
        value = session.get(key)
        if str(value or "").strip():
            lines.append(f"- {key}: {_inline_summary(value)}")
    return lines


def _inline_summary(value: Any, limit: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "...<truncated>"


__all__ = ["structured_read_summary"]
