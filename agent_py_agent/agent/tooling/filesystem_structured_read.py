# LLM: Structured filesystem reads keep control-plane JSON useful without flooding prompts.
# 模块用途: 为 read_file 提供可扩展的结构化摘要策略，目前用于子代理 latest_continue_packet.json。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _bundled_filesystem_param


# LLM: structured_read_summary dispatches special machine files to compact renderers.
# 函数用途: 对已知控制文件返回摘要；普通文件或显式按行读取时返回空字符串，交给 read_file 原逻辑处理。
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


# LLM: _has_explicit_line_range preserves precise reads when a caller asks for exact file lines.
# 函数用途: 判断 read_file 是否显式传了 start_line/end_line；传了就不做结构化摘要。
def _has_explicit_line_range(params: dict[str, Any]) -> bool:
    return (
        _bundled_filesystem_param(params, "start_line") is not None
        or _bundled_filesystem_param(params, "end_line") is not None
    )


# LLM: _subagent_continue_packet_summary extracts the facts a runner needs before deciding to continue work.
# 函数用途: 把子代理 continue packet 压成可读摘要，保留关键 refs，不复制整包所有路径。
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


# LLM: _subagent_packet_progress_lines keeps task progress prominent and compact.
# 函数用途: 渲染 work_progress 摘要；没有进度时明确输出 none。
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


# LLM: _subagent_packet_session_lines exposes session compact refs without expanding restore_refs.
# 函数用途: 渲染 session_compact 的关键 metadata/summary ref；空包只输出 none。
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


# LLM: _inline_summary keeps structured read fields one-line and bounded.
# 函数用途: 将 JSON 字段变成短行文本，避免路径数组或对象撑大 read_file 输出。
def _inline_summary(value: Any, limit: int = 500) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "...<truncated>"


__all__ = ["structured_read_summary"]
