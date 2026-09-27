# LLM: 文本与native共用此模型投影；只在临时副本移除本次归档物理ref，正文策略和canonical执行事实保持。
# 模块用途: 将工具结果压成模型上下文，以原逻辑锚点续读；核验与进程事实复用 tooling，不改归档或原结果。

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


# LLM: 保持外置摘要选择原样，直接展示分支才省略自归档ref；原始refs供归档与Compact使用，同一投影字符串绑定native结果。
# 函数用途: 整理本次工具正文、逻辑恢复引用和执行事实，只修改模型临时视图，不写原始结果。
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


# LLM: 仅精确匹配本次归档的typed tool_output引用；source_artifact_ref可能是reader的来源逻辑ref，不能滤掉。
# 同步检查原生工具历史、归档登记和Compact消费者；不可原地改canonical结果，也不扫描替换正文中的路径。
# 函数用途: 从模型临时副本去掉归档文件的重复展示，保留既有续读锚点、业务引用及原始审计数据。
def _without_own_archive_refs(result: ToolResult, archive_record: dict[str, object]) -> ToolResult:
    archive_paths = {
        str(archive_record.get(key) or "").strip()
        for key in ("output_path", "artifact_ref")
    } - {""}
    own_refs = {ref.ref for ref in result.refs
                if ref.kind == "tool_output" and ref.ref in archive_paths}
    if not own_refs:
        return result
    return replace(
        result,
        refs=tuple(ref for ref in result.refs
                   if not (ref.kind == "tool_output" and ref.ref in own_refs)),
        content_blocks=tuple(block for block in result.content_blocks
                             if not (block.type == "ref" and block.ref in own_refs)),
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


# LLM: 只在直接展示副本省略本次物理自归档ref，外置摘要选择不受影响；read_artifact继续原source ref，不能自读包装结果。
# 函数用途: 给普通内联结果保留唯一逻辑续读锚点；artifact读取直接保留自己的分页合同，不改变原refs。
def _inline_result_with_archive_anchor(result: ToolResult, archive_record: dict[str, object]) -> str:
    rendered = _without_own_archive_refs(result, archive_record).render_for_prompt()
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
