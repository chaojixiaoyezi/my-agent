from __future__ import annotations

"""LLM contract: markdown renderers for subagent reports and reviews."""

from .rendering_dispatch import (
    render_dispatch_markdown,
    render_dispatch_watch_markdown,
    render_parent_planner_markdown,
)
from .rendering_patch import (
    render_patch_apply_markdown,
    render_patch_apply_record_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from .reports import (
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    ActionApplyReport,
    ActionPlanReport,
    CapabilityRouteReport,
    DueCheckReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


def _summary_lines(summary: dict[str, int]) -> list[str]:
    return [f"- {key}: {summary[key]}" for key in sorted(summary)]


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
        if action.would_change_status_to:
            lines.append(f"  - would_change_status_to: {action.would_change_status_to}")
        if action.suggested_commands:
            lines.append("  - suggested_commands:")
            lines.extend(f"    - `{command}`" for command in action.suggested_commands[:5])
    return "\n".join(lines) + "\n"


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


def render_acceptance_review_markdown(report: AcceptanceReviewReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    lines = [
        "# SUBAGENT ACCEPTANCE",
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
        lines.append("- 暂无等待验收的子代理运行")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` decision={record.decision} "
            f"applied={record.applied} {record.before_status}/{record.before_verification_status}"
            f" -> {record.after_status}/{record.after_verification_status}"
        )
        lines.append(f"  - {record.message}")
        failed = [item for item in record.findings if not item.ok and item.severity != "P2"]
        for item in failed[:5]:
            lines.append(f"  - [{item.severity}] {item.name}: {item.message}")
    return "\n".join(lines) + "\n"


def render_acceptance_record_markdown(record: AcceptanceReviewRecord) -> str:
    lines = [
        "# ACCEPTANCE REVIEW",
        "",
        f"- id: {record.id}",
        f"- run_id: {record.run_id}",
        f"- mode: {'dry-run' if record.dry_run else 'apply'}",
        f"- decision: {record.decision}",
        f"- ok: {record.ok}",
        f"- applied: {record.applied}",
        f"- reviewer: {record.reviewer or 'none'}",
        f"- note: {record.note or 'none'}",
        f"- status: {record.before_status}/{record.before_verification_status} -> {record.after_status}/{record.after_verification_status}",
        f"- message: {record.message}",
        "",
        "## Counts",
        "",
        f"- evidence: {record.evidence_count}",
        f"- tests: {record.test_count}",
        f"- artifacts: {record.artifact_count}",
        "",
        "## Findings",
        "",
    ]
    if not record.findings:
        lines.append("- none")
    for item in record.findings:
        status = "OK" if item.ok else "FAIL"
        lines.append(f"- [{status}] {item.severity} {item.name}: {item.message}")
        if item.evidence_path:
            lines.append(f"  - evidence: {item.evidence_path}")
    return "\n".join(lines) + "\n"


def _render_board_line(item: SubAgentBoardItem) -> str:
    flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
    goal = item.goal.replace("\n", " ")[:100]
    return (
        f"- `{item.id}` status={item.status} verify={item.verification_status} "
        f"channel={item.channel_status} "
        f"depth={item.depth} owner={item.owner or 'none'} final={item.final_owner or 'none'} "
        f"evidence={item.evidence_count} requests={item.open_request_count} "
        f"gaps={item.open_gap_count} flags={flags} :: {goal}"
    )
