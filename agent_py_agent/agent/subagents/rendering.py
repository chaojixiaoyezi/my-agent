"""LLM contract: markdown renderers for subagent reports and reviews."""

from __future__ import annotations

from .reports import (
    ActionApplyReport,
    ActionPlanReport,
    CapabilityRouteReport,
    DispatchReport,
    DispatchWatchReport,
    DueCheckReport,
    ParentPlannerReport,
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


def _summary_lines(summary: dict[str, int]) -> list[str]:
    return [f"- {key}: {summary[key]}" for key in sorted(summary)]


def render_dispatch_markdown(report: DispatchReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT DISPATCH",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    lines.extend(_summary_lines(report.summary))
    lines.extend(_dispatch_completion_gate_lines(report.records))
    lines.extend(["", "## Records", ""])
    if not report.records:
        lines.append("- 暂无调度动作")
    for record in report.records[:200]:
        status = "OK" if record.ok else "FAIL"
        run = f"`{record.run_id}`" if record.run_id else "`global`"
        lines.append(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} dry_run={record.dry_run}"
        )
        lines.append(f"  - {record.message}")
        if record.runner_summary or record.runner_created_child_count:
            lines.append(_dispatch_runner_line(record))
    return "\n".join(lines) + "\n"


# LLM: This rendering is soft parent guidance only; it may name direct-child
# controls but never a polling/inspection tool or a machine quality verdict.
# 函数用途: 把未解决的 dispatch 记录渲染成父级可读的下一步提示。
def _dispatch_completion_gate_lines(records: list[object]) -> list[str]:
    blockers = _dispatch_blocking_run_ids(records)
    lines = ["", "## Completion Gate", ""]
    if not blockers:
        lines.extend([
            "- status: complete_or_no_blockers",
            "- completion_risk: false",
            "- blocking_run_ids: (none)",
        ])
        return lines
    lines.extend([
        "- status: not_complete",
        "- completion_risk: true",
        f"- blocking_run_ids: {', '.join(blockers)}",
        "- next_action: continue parent work, guide/cancel a direct child, create an explicit replacement, or explain unresolved runs before final user-facing completion.",
    ])
    return lines


def _dispatch_blocking_run_ids(records: list[object]) -> list[str]:
    ids: list[str] = []
    for record in records:
        if bool(getattr(record, "ok", True)):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids[:20]


def _dispatch_runner_line(record) -> str:
    child_ids = ",".join(record.runner_created_child_ids)
    roles = ",".join(record.runner_created_roles)
    unfinished = ",".join(record.runner_unfinished_child_ids)
    return (
        "  - runner_effect: "
        f"created_child_count={record.runner_created_child_count} "
        f"created_child_ids={child_ids} "
        f"created_roles={roles} "
        f"child_status_counts={record.runner_child_status_counts} "
        f"unfinished_child_ids={unfinished} "
        f"partial_success={record.runner_partial_success} "
        f"summary={record.runner_summary}"
    )


def render_dispatch_watch_markdown(report: DispatchWatchReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT DISPATCH WATCH",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_cycles: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Cycles",
        "",
    ]
    if not report.records:
        lines.append("- 暂无 watch 循环记录")
    for record in report.records[:200]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] cycle={record.cycle} records={record.dispatch_record_count} "
            f"started={record.started_at} ended={record.ended_at}"
        )
        lines.append(f"  - {record.message}")
    return "\n".join(lines) + "\n"


def render_parent_planner_markdown(report: ParentPlannerReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# PARENT PLANNER",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Records",
        "",
    ]
    if not report.records:
        lines.append("- 暂无 planner 记录")
    for record in report.records[:200]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] {record.id} decision={record.decision} "
            f"triggered={record.triggered} tool_rounds={record.tool_rounds}"
        )
        lines.append(f"  - {record.message}")
        if record.summary:
            lines.append(f"  - summary: {record.summary}")
        if record.runner_instruction:
            lines.append(f"  - runner_instruction: {record.runner_instruction}")
        if record.actions:
            lines.append(f"  - actions: {len(record.actions)}")
        if record.parse_error:
            lines.append(f"  - parse_error: {record.parse_error}")
        if record.gate_summary:
            gate = ", ".join(f"{key}={value}" for key, value in sorted(record.gate_summary.items()))
            lines.append(f"  - gate: {gate}")
    return "\n".join(lines) + "\n"


def render_patch_review_markdown(report: PatchReviewReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT PATCH REVIEW",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Records",
        "",
    ]
    if not report.records:
        lines.append("- none")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} blocked={record.blocked_count}"
        )
        lines.append(f"  - {record.message}")
        lines.extend(_load_error_lines(record.load_errors, prefix="  - "))
    return "\n".join(lines) + "\n"


def render_patch_review_record_markdown(record: PatchReviewRecord) -> str:
    lines = [
        "# PATCH REVIEW",
        "",
        f"- id: {record.id}",
        f"- run_id: {record.run_id}",
        f"- mode: {'dry-run' if record.dry_run else 'apply'}",
        f"- decision: {record.decision}",
        f"- ok: {record.ok}",
        f"- applied: {record.applied}",
        f"- reviewer: {record.reviewer or 'none'}",
        f"- note: {record.note or 'none'}",
        f"- patch_count: {record.patch_count}",
        f"- approved_count: {record.approved_count}",
        f"- blocked_count: {record.blocked_count}",
        f"- message: {record.message}",
        "",
        "## Load Errors",
        "",
        *_load_error_lines(record.load_errors),
        "",
        "## Patches",
        "",
        *_patch_review_lines(record.patches),
    ]
    return "\n".join(lines) + "\n"


def _patch_review_lines(patches: list[dict[str, object]]) -> list[str]:
    if not patches:
        return ["- none"]
    return [
        f"- [{item.get('status', 'unknown')}] {item.get('path', 'unknown')} "
        f"review={item.get('review_status', 'UNREVIEWED')} :: {item.get('summary', '')}"
        for item in patches
    ]


def render_patch_apply_markdown(report: PatchApplyReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT PATCH APPLY",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Records",
        "",
    ]
    if not report.records:
        lines.append("- none")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` decision={record.decision} "
            f"patches={record.patch_count} applied={record.applied_count} "
            f"blocked={record.blocked_count} rollback={record.rollback_performed}"
        )
        lines.append(f"  - {record.message}")
        lines.extend(_load_error_lines(record.load_errors, prefix="  - "))
    return "\n".join(lines) + "\n"


def render_patch_apply_record_markdown(record: PatchApplyRecord) -> str:
    lines = [
        "# PATCH APPLY",
        "",
        f"- id: {record.id}",
        f"- run_id: {record.run_id}",
        f"- mode: {'dry-run' if record.dry_run else 'apply'}",
        f"- decision: {record.decision}",
        f"- ok: {record.ok}",
        f"- applied: {record.applied}",
        f"- applied_count: {record.applied_count}",
        f"- blocked_count: {record.blocked_count}",
        f"- rollback_performed: {record.rollback_performed}",
        f"- applier: {record.applier or 'none'}",
        f"- note: {record.note or 'none'}",
        f"- message: {record.message}",
        "",
        "## Load Errors",
        "",
        *_load_error_lines(record.load_errors),
        "",
        "## Test Commands",
        "",
    ]
    lines.extend(f"- {item}" for item in record.test_commands or ["none"])
    lines.extend(["", "## Patches", "", *_patch_apply_lines(record.patches)])
    return "\n".join(lines) + "\n"


def _patch_apply_lines(patches: list[dict[str, object]]) -> list[str]:
    if not patches:
        return ["- none"]
    return [
        f"- [{item.get('apply_status', 'UNKNOWN')}] {item.get('path', 'unknown')} "
        f"status={item.get('status', 'unknown')} review={item.get('review_status', 'UNREVIEWED')} :: "
        f"{item.get('message', item.get('summary', ''))}"
        for item in patches
    ]


def _load_error_lines(load_errors: list[dict[str, object]], *, prefix: str = "") -> list[str]:
    if not load_errors:
        return [f"{prefix}- none"] if not prefix else []
    lines = []
    for item in load_errors:
        context = item.get("context", "unknown")
        path = item.get("path", "")
        message = item.get("message", item.get("error_type", ""))
        lines.append(f"{prefix}- {context}: {message} path={path}")
    return lines


def render_board_markdown(board: SubAgentBoard) -> str:
    lines = [
        "# SUBAGENT BOARD",
        "",
        f"- generated_at: {board.generated_at}",
        f"- total: {board.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(board.summary),
        "",
        "## Hot List",
        "",
    ]
    lines.extend(_board_lines(board.hot_list[:50], empty="- 暂无红灯任务"))
    lines.extend(["", "## Recent", ""])
    lines.extend(_board_lines(board.recent, empty="- 暂无任务"))
    return "\n".join(lines) + "\n"


def _board_lines(items: list[SubAgentBoardItem], *, empty: str) -> list[str]:
    return [_render_board_line(item) for item in items] if items else [empty]


def render_due_check_markdown(report: DueCheckReport) -> str:
    lines = [
        "# SUBAGENT DUE CHECK",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_issues: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Issues",
        "",
    ]
    if not report.issues:
        lines.append("- 暂无需要介入的问题")
    for issue in report.issues[:100]:
        goal = issue.goal.replace("\n", " ")[:100]
        flags = ",".join(issue.risk_flags) if issue.risk_flags else "ok"
        lines.append(
            f"- [{issue.severity}] `{issue.run_id}` {issue.kind} "
            f"status={issue.status} action={issue.suggested_action} "
            f"flags={flags} :: {goal}"
        )
        lines.append(f"  - {issue.message}")
    return "\n".join(lines) + "\n"


def render_action_plan_markdown(report: ActionPlanReport) -> str:
    lines = [
        "# SUBAGENT ACTION PLAN",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_actions: {report.summary.get('total', 0)}",
        "- mode: dry-run",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Actions",
        "",
    ]
    if not report.actions:
        lines.append("- 暂无建议动作")
    for action in report.actions[:100]:
        kinds = ",".join(action.source_issue_kinds)
        lines.append(
            f"- [{action.severity}] `{action.run_id}` priority={action.priority} "
            f"action={action.action} sources={kinds}"
        )
        lines.append(f"  - reason: {action.reason}")
        if action.rescue_strategy:
            lines.append(
                f"  - rescue: trigger={action.rescue_trigger or 'none'} "
                f"strategy={action.rescue_strategy} escalate={action.escalation_target or 'parent'}"
            )
        if action.rescue_context_refs:
            lines.append("  - rescue_context:")
            lines.extend(f"    - `{ref}`" for ref in action.rescue_context_refs[:5])
        if action.rescue_packet:
            lines.extend(render_action_rescue_packet_lines(action.rescue_packet))
        if action.would_change_status_to:
            lines.append(f"  - would_change_status_to: {action.would_change_status_to}")
        if action.suggested_commands:
            lines.append("  - suggested_commands:")
            lines.extend(f"    - `{command}`" for command in action.suggested_commands[:5])
    return "\n".join(lines) + "\n"


def render_action_rescue_packet_lines(packet: dict[str, object]) -> list[str]:
    retry = packet.get("retry_policy", {})
    manual = packet.get("manual_confirmation", {})
    retry_limit = retry.get("max_attempts", 0) if isinstance(retry, dict) else 0
    manual_required = manual.get("required", True) if isinstance(manual, dict) else True
    lines = [f"  - rescue_packet: retry_limit={retry_limit} manual_confirmation={manual_required}"]
    refs = packet.get("recovery_entrypoints", [])
    if isinstance(refs, list) and refs:
        lines.append("  - rescue_packet_refs:")
        lines.extend(f"    - `{ref}`" for ref in refs[:5])
    return lines


def render_action_apply_markdown(report: ActionApplyReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT ACTION APPLY",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Records",
        "",
    ]
    if not report.records:
        lines.append("- 暂无动作记录")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` action={record.action} "
            f"applied={record.applied} {record.before_status}->{record.after_status}"
        )
        lines.append(f"  - {record.message}")
        if record.rescue_strategy:
            lines.append(
                f"  - rescue: trigger={record.rescue_trigger or 'none'} "
                f"strategy={record.rescue_strategy} escalate={record.escalation_target or 'parent'}"
            )
        if record.evidence_paths:
            lines.append("  - evidence:")
            lines.extend(f"    - `{path}`" for path in record.evidence_paths[:5])
    return "\n".join(lines) + "\n"


def render_capability_route_markdown(report: CapabilityRouteReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT CAPABILITY ROUTE",
        "",
        f"- generated_at: {report.generated_at}",
        f"- mode: {mode}",
        f"- total_records: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
        *_summary_lines(report.summary),
        "",
        "## Records",
        "",
    ]
    if not report.records:
        lines.append("- 暂无待路由能力请求")
    for record in report.records[:100]:
        cards = ", ".join(
            f"{item.get('kind')}:{item.get('name')}" for item in record.selected_cards
        ) or "none"
        lines.append(
            f"- [{record.status}] `{record.run_id}` request={record.request_id} "
            f"cards={cards}"
        )
        lines.append(f"  - message: {record.message}")
        if record.reasons:
            lines.append(f"  - reasons: {'; '.join(record.reasons[:5])}")
    return "\n".join(lines) + "\n"


def _list_or_none(items: list[str]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items]


def _render_board_line(item: SubAgentBoardItem) -> str:
    flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
    children = ",".join(f"{key}:{value}" for key, value in sorted(item.child_status_counts.items())) or "none"
    goal = item.goal.replace("\n", " ")[:100]
    return (
        f"- `{item.id}` role={item.role or 'unknown'} name={item.agent_name or 'unnamed'} "
        f"status={item.status} verify={item.verification_status} "
        f"channel={item.channel_status} "
        f"depth={item.depth} owner={item.owner or 'none'} final={item.final_owner or 'none'} "
        f"progress={item.progress:.0%} evidence={item.evidence_count} "
        f"packets={item.evidence_packet_count} findings={item.finding_count} "
        f"children={item.child_count} child_status={children} blockers={item.blocker_count} "
        f"requests={item.open_request_count} gaps={item.open_gap_count} flags={flags} :: {goal}"
    )
