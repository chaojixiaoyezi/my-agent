# LLM: 正文按原工具信任策略处理，执行事实只读canonical metadata并在统一脱敏前追加；文本与native共用此入口。
# 模块用途: 将工具结果压成模型上下文；核验与进程事实复用 tooling 投影，和索引恢复同口径，不改原结果。

from __future__ import annotations

"""Live prompt reducer for tool execution results.

This is the model-facing choke point: output bodies use the ToolRuntimePolicy-derived
projection while status, verification facts, hashes and archive refs remain
structured framework facts outside an untrusted external-data body.
"""

import json
from dataclasses import replace

from ...tooling.output_projection import (
    project_tool_output_body,
    redact_tool_output_text,
)
from ...tooling.runtime_contracts import ToolContentBlock, ToolResult
from ...tooling.runtime_facts import render_tool_runtime_facts
from ..orchestration.context.live_summary import orchestration_live_summary
from .action_summary import actionable_tool_result_summary


# LLM: 所有长度分支共用事实投影和最终脱敏；不得将外部正文解析为宿主事实，调用方将同一字符串绑定native结果。
# 函数用途: 整理本次工具的安全正文、恢复引用和执行事实，不修改原始结果。
def render_tool_result_for_live_prompt(result: ToolResult, archive_record: dict[str, object]) -> str:
    live_output = _live_prompt_output(result)
    if live_output is not None:
        rendered = _inline_result_with_archive_anchor(
            replace(
                result,
                content_blocks=(
                    ToolContentBlock("text", text=_project_output_body(result, live_output)),
                ),
            ),
            archive_record,
        )
    elif _preserve_prompt_output(result) or not archive_record.get(
        "output_externalized"
    ):
        rendered = _inline_result_with_archive_anchor(
            result,
            archive_record,
        )
    else:
        rendered = _externalized_result_summary(result, archive_record)
    facts = render_tool_runtime_facts(_handler_details(result))
    if facts:
        rendered = f"{rendered}\n{facts}"
    return redact_tool_output_text(
        rendered,
        redaction=_output_redaction(result),
    )


# LLM: Externalized results prefer structured orchestration/action summaries before the generic anchor.
# 函数用途: 为外置大输出选择最有用的摘要，并保留能重新读取完整内容的稳定引用。
def _externalized_result_summary(
    result: ToolResult,
    archive_record: dict[str, object],
) -> str:
    stored_summary = str(archive_record.get("model_summary") or "").strip()
    if stored_summary:
        return stored_summary
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


def _live_prompt_output(result: ToolResult) -> str | None:
    policy = _handler_details(result).get("tool_output_policy")
    if not isinstance(policy, dict) or "live_prompt_output" not in policy:
        return None
    value = policy.get("live_prompt_output")
    return str(value) if value is not None else ""


def _preserve_prompt_output(result: ToolResult) -> bool:
    policy = _handler_details(result).get("tool_output_policy")
    return isinstance(policy, dict) and bool(policy.get("preserve_prompt_output"))


def _handler_details(result: ToolResult) -> dict[str, object]:
    details = result.metadata.get("handler_details")
    return dict(details) if isinstance(details, dict) else {}


def _project_output_body(result: ToolResult, output: str) -> str:
    return project_tool_output_body(
        tool=result.tool_name,
        output=output,
        trust=result.output_trust,
        redaction=result.output_redaction,
    )


def _output_trust(result: ToolResult) -> str:
    return result.output_trust


def _output_redaction(result: ToolResult) -> str:
    return result.output_redaction


# LLM: read_artifact already returns its source logical ref and exact next_read window. Appending a
# second anchor for the reader call creates recursive recovery and must be skipped.
# 函数用途: 给普通内联工具结果补归档锚点；artifact 读取结果直接保留自己的分页合同。
def _inline_result_with_archive_anchor(result: ToolResult, archive_record: dict[str, object]) -> str:
    rendered = result.render_for_prompt()
    if result.tool_name == "read_artifact":
        return rendered
    artifact_ref = str(archive_record.get("artifact_ref") or archive_record.get("source_artifact_ref") or "").strip()
    scoped_call_id = str(archive_record.get("scoped_call_id") or "").strip()
    if not artifact_ref:
        return rendered
    lines = [
        rendered,
        "[tool-output-archive-anchor]",
        f"- output_scoped_call_id: {scoped_call_id}",
        "- artifact_ref_policy: the physical blob path is host-only; use output_scoped_call_id/read_artifact if more detail is needed.",
    ]
    if scoped_call_id:
        lines.append(f"- read_artifact_hint: {_read_artifact_hint(scoped_call_id, archive_record)}")
    return "\n".join(lines)


# LLM: Model calls carry only the logical ref; run/task/request identity is host-injected and must
# not be copied out of prompt text as if it were model authority.
# 函数用途: 生成模型可以直接照抄的最小 read_artifact 调用参数。
def _read_artifact_hint(ref: str, archive_record: dict[str, object]) -> str:
    # Runtime identity is injected by the host and is intentionally absent from the model call.
    payload = {
        "tool": "read_artifact",
        "artifact_ref": ref,
        "offset": 0,
        "max_chars": 4000,
    }
    return json.dumps(payload, ensure_ascii=False)
