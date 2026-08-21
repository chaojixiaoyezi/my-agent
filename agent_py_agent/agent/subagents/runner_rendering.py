
from __future__ import annotations

from ..common.value_parsing import dedupe_strings
from .context_bundle import context_gate_prompt_lines
from .models import (
    ChannelProbeReport,
    ChannelProbeResult,
    SubAgentExecutionContext,
)
from .runner_rendering_context_packs import render_context_packs_section
from .runner_result_rendering import render_runner_result_markdown


def _render_execution_context_header(context):
    return [
        "# SUBAGENT EXECUTION CONTEXT",
        "",
        f"- run_id: {context.run_id}",
        f"- generated_at: {context.generated_at}",
        f"- status: {context.status}",
        f"- turn_end_reason: {context.turn_end_reason or 'none'}",
        f"- channel_status: {context.channel_status}",
        f"- runner_attempts: {context.runner_attempts}",
        f"- runner_last_error: {context.runner_last_error or 'none'}",
        f"- agent: {context.agent_name}",
        f"- role: {context.role}",
        f"- owner: {context.owner or 'none'}",
        f"- supervisor: {context.supervisor or 'none'}",
        f"- final_owner: {context.final_owner or 'none'}",
        f"- parent_id: {context.parent_id or 'none'}",
        f"- root_id: {context.root_id or context.run_id}",
        f"- depth: {context.depth}",
        f"- subagent_session_id: {context.subagent_session_id or 'none'}",
        f"- agent_thread_id: {context.agent_thread_id or 'none'}",
        f"- parent_subagent_session_id: {context.parent_subagent_session_id or 'none'}",
        f"- root_subagent_session_id: {context.root_subagent_session_id or context.subagent_session_id or 'none'}",
        f"- task_dir: {context.task_dir}",
        "",
        "## Goal",
        "",
        context.goal,
        "",
        "## Thought",
        "",
        context.thought or "未设置",
        "",
        "## Plan",
        "",
    ]


def _render_context_bundle_section(context: SubAgentExecutionContext) -> list[str]:
    refs = context.context_bundle.get("workspace_refs") if isinstance(context.context_bundle, dict) else {}
    if not isinstance(refs, dict):
        refs = {}
    lines = [
        "",
        "## Context Bundle",
        "",
        f"- context_bundle_json: {context.context_bundle_json or 'none'}",
        f"- context_bundle_file: {context.context_bundle_file or 'none'}",
        f"- owner_workspace_dir: {refs.get('owner_workspace_dir') or 'none'}",
        f"- task_root: {refs.get('task_root') or context.task_dir or 'none'}",
        f"- agent_work_dir: {refs.get('agent_work_dir') or 'none'}",
        f"- agent_run_context_bundle_json: {_run_workspace_bundle_ref(refs, 'context_bundle.json')}",
        f"- agent_run_context_bundle_file: {_run_workspace_bundle_ref(refs, 'CONTEXT_BUNDLE.md')}",
    ]
    lines.extend(context_gate_prompt_lines(context.context_bundle))
    return lines


def _run_workspace_bundle_ref(refs: dict[str, object], name: str) -> str:
    workspace = str(refs.get("agent_work_dir") or refs.get("agent_run_workspace") or "").strip()
    if not workspace:
        return "none"
    return f"{workspace.rstrip('/')}/{name}"


def _render_capabilities_section(context):
    lines = ["## Allowed Capabilities", ""]
    lines.append(f"- skills: {', '.join(context.allowed_skills) or 'none'}")
    lines.append(f"- tools: {', '.join(context.allowed_tools) or 'none'}")
    shell_mode = ""
    if isinstance(context.effective_permissions, dict):
        shell_mode = str(context.effective_permissions.get("shell_access_mode") or "").strip()
    if shell_mode:
        lines.append(f"- shell_access_mode: {shell_mode}")
    lines.extend(["", "## Granted Cards", ""])
    if context.granted_cards:
        for card in context.granted_cards:
            lines.extend(_render_granted_card_lines(card))
    else:
        lines.append("- none")
    return lines


def _render_granted_card_lines(card: dict[str, object]) -> list[str]:
    lines = [
        f"- [{card.get('kind', 'unknown')}] {card.get('name', 'unknown')} "
        f"risk={card.get('risk_level', 'unknown')} source={card.get('source', 'unknown')}"
    ]
    if card.get("description"):
        lines.append(f"  - description: {card['description']}")
    if card.get("path"):
        lines.append(f"  - path: {card['path']}")
    if card.get("reasons"):
        lines.append(f"  - reasons: {card['reasons']}")
    return lines


def _render_write_boundary_section(context):
    lines = ["", "## Write Boundary", ""]
    allowed_roots = context.write_boundary.get("allowed_write_roots") or []
    product_roots = context.write_boundary.get("product_write_roots") or []
    forbidden_roots = context.write_boundary.get("forbidden_write_roots") or []
    locked_files = context.write_boundary.get("locked_files") or []
    lines.append(f"- task_dir: {context.write_boundary.get('task_dir') or context.task_dir}")
    lines.append(f"- allowed_write_roots: {', '.join(allowed_roots) if allowed_roots else 'none'}")
    lines.append(f"- product_write_roots: {', '.join(product_roots) if product_roots else 'none'}")
    lines.append(f"- product_write_policy: {context.write_boundary.get('product_write_policy') or 'direct'}")
    if context.write_boundary.get("shell_access_mode"):
        lines.append(f"- shell_access_mode: {context.write_boundary.get('shell_access_mode')}")
    lines.append(
        f"- forbidden_write_roots: {', '.join(forbidden_roots) if forbidden_roots else 'none'}"
    )
    lines.append(f"- locked_files: {', '.join(locked_files) if locked_files else 'none'}")
    return lines


def _render_evidence_item_lines(item: dict[str, object]) -> list[str]:
    status = "OK" if item.get("ok") else "FAIL"
    lines = [f"- [{status}] {item.get('kind', 'unknown')}: {item.get('summary', '')}"]
    if item.get("command"):
        lines.append(f"  - command: `{item['command']}`")
    if item.get("path"):
        lines.append(f"  - path: {item['path']}")
    if item.get("url"):
        lines.append(f"  - url: {item['url']}")
    return lines


def _render_declared_outputs_section(context):
    output_contract = (
        context.context_bundle.get("output_contract")
        if isinstance(context.context_bundle, dict)
        else {}
    )
    if not isinstance(output_contract, dict):
        output_contract = {}
    required_refs = dedupe_strings(output_contract.get("required_file_refs"))
    declared_refs = dedupe_strings(output_contract.get("declared_output_refs"))
    if not required_refs and not declared_refs:
        return []
    lines = ["", "## Declared Output Targets", ""]
    if required_refs:
        lines.append("- required_file_refs:")
        lines.extend(f"  - {item}" for item in required_refs)
    if declared_refs:
        lines.append("- declared_output_refs (may include logical result keys):")
        lines.extend(f"  - {item}" for item in declared_refs)
    if required_refs:
        lines.append(
            "- 如果上面有 required_file_refs，必须把交付产物写到这些路径；"
            "内部 output.json 只能作为运行报告，不能单独冒充用户产物。"
        )
    return lines


def _render_quality_contract_section(contract):
    lines = ["", "## Quality Contract", ""]
    lines.append(f"- user_visible_goal: {contract.user_visible_goal or 'none'}")
    lines.append(f"- benchmark_sample: {contract.benchmark_sample or 'none'}")
    lines.append(f"- quality_bar: {contract.quality_bar or 'none'}")
    lines.append("- failure_conditions:")
    lines.extend(f"  - {item}" for item in contract.failure_conditions or ["none"])
    lines.append("- forbidden_delivery:")
    lines.extend(f"  - {item}" for item in contract.forbidden_delivery or ["none"])
    lines.append("- must_check:")
    lines.extend(f"  - {item}" for item in contract.must_check or ["none"])
    lines.append("- sampling_plan:")
    lines.extend(f"  - {item}" for item in contract.sampling_plan or ["none"])
    lines.append("- evidence_required:")
    lines.extend(f"  - {item}" for item in contract.evidence_required or ["none"])
    lines.append(f"- risk_report_required: {contract.risk_report_required or 'none'}")
    lines.append("- allowed_degradation:")
    lines.extend(f"  - {item}" for item in contract.allowed_degradation or ["none"])
    return lines
def _render_context_manifest_section(manifest):
    lines = ["", "## Context Manifest", ""]
    lines.append(f"- core_pack_version: {manifest.core_pack_version}")
    lines.append(f"- role_pack: {manifest.role_pack or 'none'}")
    lines.append(f"- quality_contract_ref: {manifest.quality_contract_ref or 'none'}")
    lines.append(f"- token_budget: {manifest.token_budget}")
    lines.append("- task_pack_refs:")
    lines.extend(f"  - {item}" for item in manifest.task_pack_refs or ["none"])
    lines.append("- required_read_paths:")
    lines.extend(f"  - {item}" for item in manifest.required_read_paths or ["none"])
    lines.append("- omitted_context:")
    lines.extend(f"  - {item}" for item in manifest.omitted_context or ["none"])
    return lines


def _render_evidence_section(context):
    lines = ["", "## Evidence", ""]
    if context.evidence:
        for item in context.evidence:
            lines.extend(_render_evidence_item_lines(item))
    else:
        lines.append("- 暂无")
    return lines


def _render_pending_requests_section(context):
    lines = ["", "## Pending Capability Requests", ""]
    if context.pending_requests:
        for item in context.pending_requests:
            lines.append(
                f"- `{item.get('id')}` needed={item.get('needed_capability')} "
                f"status={item.get('status')}: {item.get('problem')}"
            )
    else:
        lines.append("- none")
    return lines


def _render_open_gaps_section(context):
    lines = ["", "## Open Capability Gaps", ""]
    if context.open_gaps:
        for item in context.open_gaps:
            lines.append(
                f"- `{item.get('id')}` missing={item.get('missing_capability')} "
                f"status={item.get('status')}: {item.get('why_failed')}"
            )
    else:
        lines.append("- none")
    return lines


def render_execution_context_markdown(context: SubAgentExecutionContext) -> str:
    lines = _render_execution_context_header(context)
    lines.extend(f"- {item}" for item in context.plan or ["未设置"])
    lines.extend(_render_context_bundle_section(context))
    lines.extend(_render_capabilities_section(context))
    lines.extend(_render_write_boundary_section(context))
    lines.extend(_render_declared_outputs_section(context))
    lines.extend(_render_quality_contract_section(context.quality_contract))
    lines.extend(_render_context_manifest_section(context.context_manifest))
    lines.extend(render_context_packs_section(context))
    lines.extend(_render_evidence_section(context))
    lines.extend(_render_pending_requests_section(context))
    lines.extend(_render_open_gaps_section(context))
    lines.extend(["", "## Execution Rules", ""])
    lines.append("- Finish the assigned task, write concrete refs, and hand results back to the caller.")
    lines.extend(f"- {item}" for item in context.instructions)
    return "\n".join(lines) + "\n"


def render_channel_probe_markdown(report: ChannelProbeReport) -> str:
    lines = [
        "# SUBAGENT CHANNEL PROBE",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Results", ""])
    if not report.results:
        lines.append("- 暂无可检查的子代理记录")
    for result in report.results[:100]:
        failed = [check for check in result.checks if not check.ok]
        goal = result.goal.replace("\n", " ")[:100]
        lines.append(
            f"- `{result.run_id}` channel={result.channel_status} "
            f"failed_checks={len(failed)} :: {goal}"
        )
        for check in failed[:5]:
            lines.append(f"  - [{check.severity}] {check.name}: {check.summary} {check.error}".rstrip())
    return "\n".join(lines) + "\n"


def render_single_channel_probe_markdown(result: ChannelProbeResult) -> str:
    lines = [
        "# CHANNEL PROBE",
        "",
        f"- run_id: {result.run_id}",
        f"- channel_status: {result.channel_status}",
        f"- created_at: {result.created_at}",
        f"- task_dir: {result.task_dir}",
        "",
        "## Checks",
        "",
    ]
    for check in result.checks:
        status = "OK" if check.ok else "FAIL"
        lines.append(
            f"- [{status}] {check.name} severity={check.severity} "
            f"evidence={check.evidence_path or 'none'}"
        )
        lines.append(f"  - {check.summary}")
        if check.error:
            lines.append(f"  - error: {check.error}")
    return "\n".join(lines) + "\n"
def _render_runner_item_line(item: dict[str, object]) -> str:
    title = (
        item.get("path")
        or item.get("name")
        or item.get("command")
        or item.get("summary")
        or item.get("description")
        or "item"
    )
    details = []
    for key in ["kind", "status", "ok", "summary", "description"]:
        if key in item and item[key] not in ("", None):
            details.append(f"{key}={item[key]}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"- {title}{suffix}"
