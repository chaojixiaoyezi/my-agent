from __future__ import annotations

"""LLM contract: markdown renderers for runner context, results, and channel probes.

Human version:
runner 相关输出更偏执行现场，所以单独放在这里。这样验收报告渲染和 runner
执行上下文渲染不会互相搅在一个文件里。
"""

from .models import ChannelProbeResult, ChannelProbeReport, SubAgentExecutionContext, SubAgentRunnerResult

def render_execution_context_markdown(context: SubAgentExecutionContext) -> str:
    """渲染给子代理执行器读取的人类版上下文。"""

    lines = [
        "# SUBAGENT EXECUTION CONTEXT",
        "",
        f"- run_id: {context.run_id}",
        f"- generated_at: {context.generated_at}",
        f"- status: {context.status}",
        f"- verification_status: {context.verification_status}",
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
    lines.extend(f"- {item}" for item in context.plan or ["未设置"])
    lines.extend(
        [
            "",
            "## Allowed Capabilities",
            "",
            f"- skills: {', '.join(context.allowed_skills) or 'none'}",
            f"- tools: {', '.join(context.allowed_tools) or 'none'}",
            "",
            "## Granted Cards",
            "",
        ]
    )
    if context.granted_cards:
        for card in context.granted_cards:
            lines.append(
                f"- [{card.get('kind', 'unknown')}] {card.get('name', 'unknown')} "
                f"risk={card.get('risk_level', 'unknown')} source={card.get('source', 'unknown')}"
            )
            if card.get("description"):
                lines.append(f"  - description: {card['description']}")
            if card.get("path"):
                lines.append(f"  - path: {card['path']}")
            if card.get("reasons"):
                lines.append(f"  - reasons: {card['reasons']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Write Boundary", ""])
    allowed_roots = context.write_boundary.get("allowed_write_roots") or []
    forbidden_roots = context.write_boundary.get("forbidden_write_roots") or []
    locked_files = context.write_boundary.get("locked_files") or []
    lines.append(f"- task_dir: {context.write_boundary.get('task_dir') or context.task_dir}")
    lines.append(f"- allowed_write_roots: {', '.join(allowed_roots) if allowed_roots else 'none'}")
    lines.append(
        f"- forbidden_write_roots: {', '.join(forbidden_roots) if forbidden_roots else 'none'}"
    )
    lines.append(f"- locked_files: {', '.join(locked_files) if locked_files else 'none'}")

    lines.extend(["", "## Acceptance Checks", ""])
    lines.extend(f"- [ ] {item}" for item in context.acceptance_checks or ["未设置"])

    contract = context.quality_contract
    lines.extend(["", "## Quality Contract", ""])
    lines.append(f"- user_visible_goal: {contract.user_visible_goal or 'none'}")
    lines.append(f"- benchmark_sample: {contract.benchmark_sample or 'none'}")
    lines.append(f"- quality_bar: {contract.quality_bar or 'none'}")
    lines.append(f"- final_judge: {contract.final_judge or 'parent_final_gate'}")
    lines.append(f"- cannot_self_accept: {contract.cannot_self_accept}")
    lines.append(f"- parent_final_gate: {contract.parent_final_gate}")
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

    manifest = context.context_manifest
    lines.extend(["", "## Context Manifest", ""])
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

    lines.extend(["", "## Context Packs", ""])
    if context.context_packs:
        for item in context.context_packs:
            name = item.get("name") or item.get("id") or item.get("kind") or "pack"
            lines.append(f"- {name}")
            for key in ["kind", "summary", "path", "ref", "role"]:
                if item.get(key):
                    lines.append(f"  - {key}: {item[key]}")
    else:
        lines.append("- none")

    lines.extend(["", "## Evidence", ""])
    if context.evidence:
        for item in context.evidence:
            status = "OK" if item.get("ok") else "FAIL"
            lines.append(f"- [{status}] {item.get('kind', 'unknown')}: {item.get('summary', '')}")
            if item.get("command"):
                lines.append(f"  - command: `{item['command']}`")
            if item.get("path"):
                lines.append(f"  - path: {item['path']}")
            if item.get("url"):
                lines.append(f"  - url: {item['url']}")
    else:
        lines.append("- 暂无")

    lines.extend(["", "## Pending Capability Requests", ""])
    if context.pending_requests:
        for item in context.pending_requests:
            lines.append(
                f"- `{item.get('id')}` needed={item.get('needed_capability')} "
                f"status={item.get('status')}: {item.get('problem')}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Open Capability Gaps", ""])
    if context.open_gaps:
        for item in context.open_gaps:
            lines.append(
                f"- `{item.get('id')}` missing={item.get('missing_capability')} "
                f"status={item.get('status')}: {item.get('why_failed')}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Execution Rules", ""])
    lines.append(
        "- Subagents cannot self-accept or declare final completion; only the parent "
        "session final_judge/parent_final_gate can make the final acceptance decision."
    )
    lines.extend(f"- {item}" for item in context.instructions)
    return "\n".join(lines) + "\n"


def render_runner_result_markdown(result: SubAgentRunnerResult) -> str:
    """渲染子代理 runner 调用结果。"""

    status = "OK" if result.ok else "FAIL"
    mode = "dry-run" if result.dry_run else "execute"
    lines = [
        "# SUBAGENT RUNNER RESULT",
        "",
        f"- run_id: {result.run_id}",
        f"- created_at: {result.created_at}",
        f"- mode: {mode}",
        f"- status: {result.status}",
        f"- verification_status: {result.verification_status}",
        f"- ok: {status}",
        f"- backend: {result.backend or 'none'}",
        f"- tool_rounds: {result.tool_rounds}",
        f"- runner_attempts: {result.runner_attempts}",
        f"- runner_last_error: {result.runner_last_error or 'none'}",
        f"- structured_output_found: {result.structured_output_found}",
        f"- structured_output_ok: {result.structured_output_ok}",
        f"- structured_repair_attempted: {result.structured_repair_attempted}",
        f"- structured_repair_ok: {result.structured_repair_ok}",
        f"- evidence_count: {result.evidence_count}",
        f"- capability_request_count: {result.capability_request_count}",
        f"- artifact_count: {result.artifact_count}",
        f"- test_count: {result.test_count}",
        f"- patch_count: {result.patch_count}",
        f"- lesson_count: {result.lesson_count}",
        "",
        "## Message",
        "",
        result.message or "none",
    ]
    if result.structured_summary or result.blocked_reason or result.structured_parse_error:
        lines.extend(["", "## Structured Output", ""])
        if result.structured_summary:
            lines.append(f"- summary: {result.structured_summary}")
        if result.blocked_reason:
            lines.append(f"- blocked_reason: {result.blocked_reason}")
        if result.structured_parse_error:
            lines.append(f"- parse_error: {result.structured_parse_error}")
        if result.structured_repair_error:
            lines.append(f"- repair_error: {result.structured_repair_error}")
    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- execution_context_json: {result.execution_context_json}",
            f"- execution_context_file: {result.execution_context_file}",
            f"- prompt_file: {result.prompt_file or 'none'}",
            f"- response_file: {result.response_file or 'none'}",
            f"- result_json: {result.result_json}",
            f"- output_json: {result.output_json}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_channel_probe_markdown(report: ChannelProbeReport) -> str:
    """渲染批量通道健康检查报告。"""

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
    """渲染单个 run 的通道健康检查证据。"""

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
    """把 runner 结构化条目渲染成一行 debrief。"""

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
