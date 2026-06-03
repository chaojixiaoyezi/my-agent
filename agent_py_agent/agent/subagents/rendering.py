
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
from .rendering_rescue import render_action_rescue_packet_lines
from .reports import (
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
