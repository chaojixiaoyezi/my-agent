
from __future__ import annotations

"""Live prompt reducer for tool execution results."""

import json

from ...tools import ToolExecutionResult
from ..orchestration.context.live_summary import orchestration_live_summary
from .action_summary import actionable_tool_result_summary


def render_tool_result_for_live_prompt(result: ToolExecutionResult, archive_record: dict[str, object]) -> str:
    if not archive_record.get("output_externalized"):
        return result.render_for_prompt()
    orchestration_summary = orchestration_live_summary(result, archive_record)
    if orchestration_summary:
        return orchestration_summary
    action_summary = actionable_tool_result_summary(result, archive_record)
    if action_summary:
        return action_summary
    status = "ok" if result.ok else "error"
    artifact_ref = str(archive_record.get("artifact_ref") or archive_record.get("output_path") or "")
    call_id = str(archive_record.get("id") or "")
    lines = [
        f"[tool={result.tool}; status={status}]",
        "完整工具输出已外置，live prompt 只保留摘要和恢复锚点。",
        f"- output_preview: {archive_record.get('output_preview', '')}",
        f"- output_path: {archive_record.get('output_path', '')}",
        f"- output_artifact_ref: {artifact_ref}",
        f"- output_call_id: {call_id}",
        f"- output_scoped_call_id: {archive_record.get('scoped_call_id', '')}",
        f"- read_artifact_hint: {_read_artifact_hint(artifact_ref or call_id, archive_record)}",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    checkpoint = str(archive_record.get("fail_safe_checkpoint_path") or "")
    if checkpoint:
        lines.append(f"- fail_safe_checkpoint: {checkpoint}")
    return "\n".join(lines)


def _read_artifact_hint(ref: str, archive_record: dict[str, object]) -> str:
    payload = {"tool": "read_artifact", "artifact_ref": ref, "offset": 0, "max_chars": 4000}
    for key in ("run_id", "task_id", "request_id"):
        value = str(archive_record.get(key) or "")
        if value:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)
