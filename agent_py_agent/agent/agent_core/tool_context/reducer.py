
from __future__ import annotations

"""Live prompt reducer for tool execution results.

This is the model-facing choke point: output bodies use the ToolSpec-derived
projection while status, verification facts, hashes and archive refs remain
structured framework facts outside an untrusted external-data body.
"""

import json
from dataclasses import replace

from ...tooling.models import ToolExecutionResult
from ...tooling.output_projection import (
    model_tool_output_body,
    redact_tool_output_text,
    tool_output_projection_policy,
)
from ..orchestration.context.live_summary import orchestration_live_summary
from .action_summary import actionable_tool_result_summary


def render_tool_result_for_live_prompt(result: ToolExecutionResult, archive_record: dict[str, object]) -> str:
    live_output = _live_prompt_output(result)
    if live_output is not None:
        rendered = _inline_result_with_archive_anchor(
            replace(result, output=_project_output_body(result, live_output)),
            archive_record,
        )
    elif not archive_record.get("output_externalized"):
        rendered = _inline_result_with_archive_anchor(
            replace(result, output=_project_output_body(result, result.output)),
            archive_record,
        )
    else:
        rendered = _externalized_result_summary(result, archive_record)
    rendered = _with_verification_facts(rendered, result)
    return redact_tool_output_text(
        rendered,
        redaction=_output_redaction(result),
    )


# LLM: Externalized results prefer structured orchestration/action summaries before the generic anchor.
# 函数用途: 为外置大输出选择最有用的摘要，并保留能重新读取完整内容的稳定引用。
def _externalized_result_summary(
    result: ToolExecutionResult,
    archive_record: dict[str, object],
) -> str:
    if _output_trust(result) != "external_data":
        orchestration_summary = orchestration_live_summary(result, archive_record)
        if orchestration_summary:
            return orchestration_summary
        action_summary = actionable_tool_result_summary(result, archive_record)
        if action_summary:
            return action_summary
    artifact_ref = str(archive_record.get("artifact_ref") or archive_record.get("output_path") or "")
    call_id = str(archive_record.get("id") or "")
    scoped_call_id = str(archive_record.get("scoped_call_id") or "")
    preview = _project_output_body(
        result,
        str(archive_record.get("output_preview") or ""),
    )
    lines = [
        result.render_status_header(),
        "完整工具输出已外置，live prompt 只保留摘要和恢复锚点。",
        f"- output_preview:\n{preview}",
        f"- output_call_id: {call_id}",
        f"- output_scoped_call_id: {scoped_call_id}",
        "- artifact_ref_policy: use output_scoped_call_id for read_artifact; absolute blob paths are archival only.",
        f"- read_artifact_hint: {_read_artifact_hint(scoped_call_id or call_id or artifact_ref, archive_record)}",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    checkpoint = str(archive_record.get("fail_safe_checkpoint_path") or "")
    if checkpoint:
        lines.append(f"- fail_safe_checkpoint: {checkpoint}")
    # Execution facts are host-owned metadata; never merge them into the projected tool output body.
    lines.append(result.render_execution_facts())
    return "\n".join(lines)


def _with_verification_facts(rendered: str, result: ToolExecutionResult) -> str:
    facts = {
        key: result.result_envelope[key]
        for key in ("verification_evidence", "verification_state")
        if key in result.result_envelope
    }
    if not facts:
        return rendered
    return (
        f"{rendered}\n[runtime-verification-facts]\n"
        "These facts come from executed commands and structured file-write events; "
        "use them when reporting test scope/status, and do not quote this internal label to the user.\n"
        f"{json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
    )


def _live_prompt_output(result: ToolExecutionResult) -> str | None:
    policy = result.result_envelope.get("tool_output_policy")
    if not isinstance(policy, dict) or "live_prompt_output" not in policy:
        return None
    value = policy.get("live_prompt_output")
    return str(value) if value is not None else ""


def _project_output_body(result: ToolExecutionResult, output: str) -> str:
    return model_tool_output_body(
        tool=result.tool,
        output=output,
        result_envelope=result.result_envelope,
    )


def _output_trust(result: ToolExecutionResult) -> str:
    return tool_output_projection_policy(result.result_envelope)[0]


def _output_redaction(result: ToolExecutionResult) -> str:
    return tool_output_projection_policy(result.result_envelope)[1]


def _inline_result_with_archive_anchor(result: ToolExecutionResult, archive_record: dict[str, object]) -> str:
    rendered = result.render_for_prompt()
    artifact_ref = str(archive_record.get("artifact_ref") or archive_record.get("source_artifact_ref") or "").strip()
    scoped_call_id = str(archive_record.get("scoped_call_id") or "").strip()
    if not artifact_ref:
        return rendered
    lines = [
        rendered,
        "[tool-output-archive-anchor]",
        f"- output_scoped_call_id: {scoped_call_id}",
        f"- artifact_ref: {artifact_ref}",
        "- artifact_ref_policy: live prompt kept the bounded tool output inline; use output_scoped_call_id/read_artifact only if more detail is needed.",
    ]
    if scoped_call_id:
        lines.append(f"- read_artifact_hint: {_read_artifact_hint(scoped_call_id, archive_record)}")
    return "\n".join(lines)


def _read_artifact_hint(ref: str, archive_record: dict[str, object]) -> str:
    payload = {"tool": "read_artifact", "artifact_ref": ref, "offset": 0, "max_chars": 4000}
    for key in ("run_id", "task_id", "request_id"):
        value = str(archive_record.get(key) or "")
        if value:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False)
