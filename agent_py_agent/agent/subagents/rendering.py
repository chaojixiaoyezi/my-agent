from __future__ import annotations

"""LLM contract: markdown renderers for subagent reports and reviews.

Human version:
这里专门把机器报告变成人能扫视的 Markdown。它不负责生成报告事实，
只负责展示格式，避免管理器里混入一堆字符串拼接。
"""

from .reports import (
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    ActionApplyReport,
    ActionPlanReport,
    CapabilityRouteReport,
    DispatchReport,
    DispatchWatchReport,
    DueCheckReport,
    ParentPlannerReport,
    PatchReviewRecord,
    PatchReviewReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


def render_board_markdown(board: SubAgentBoard) -> str:
    """渲染人类可扫视的红绿灯看板。"""

    lines = [
        "# SUBAGENT BOARD",
        "",
        f"- generated_at: {board.generated_at}",
        f"- total: {board.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(board.summary):
        lines.append(f"- {key}: {board.summary[key]}")
    lines.extend(["", "## Hot List", ""])
    if board.hot_list:
        for item in board.hot_list[:50]:
            lines.append(_render_board_line(item))
    else:
        lines.append("- 暂无红灯任务")
    lines.extend(["", "## Recent", ""])
    if board.recent:
        for item in board.recent:
            lines.append(_render_board_line(item))
    else:
        lines.append("- 暂无任务")
    return "\n".join(lines) + "\n"


def render_due_check_markdown(report: DueCheckReport) -> str:
    """渲染父代理 due-check 报告。"""

    lines = [
        "# SUBAGENT DUE CHECK",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_issues: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Issues", ""])
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
    """渲染 dry-run 动作计划。"""

    lines = [
        "# SUBAGENT ACTION PLAN",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total_actions: {report.summary.get('total', 0)}",
        "- mode: dry-run",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Actions", ""])
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
            for command in action.suggested_commands[:5]:
                lines.append(f"    - `{command}`")
    return "\n".join(lines) + "\n"


def render_action_apply_markdown(report: ActionApplyReport) -> str:
    """渲染 action apply 报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
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
            for path in record.evidence_paths[:5]:
                lines.append(f"    - `{path}`")
    return "\n".join(lines) + "\n"


def render_capability_route_markdown(report: CapabilityRouteReport) -> str:
    """渲染 capability request 路由报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
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
    """渲染批量验收报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
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
    """渲染单个验收记录。"""

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
    for item in record.findings:
        status = "OK" if item.ok else "FAIL"
        lines.append(f"- [{status}] {item.severity} {item.name}: {item.message}")
        if item.evidence_path:
            lines.append(f"  - evidence: {item.evidence_path}")
    return "\n".join(lines) + "\n"


def render_patch_review_markdown(report: PatchReviewReport) -> str:
    """渲染批量 patch 审核报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
    if not report.records:
        lines.append("- 暂无 patch 需要审核")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` decision={record.decision} "
            f"patches={record.patch_count} approved={record.approved_count} blocked={record.blocked_count}"
        )
        lines.append(f"  - {record.message}")
    return "\n".join(lines) + "\n"


def render_patch_review_record_markdown(record: PatchReviewRecord) -> str:
    """渲染单个 patch 审核记录。"""

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
        "## Patches",
        "",
    ]
    if not record.patches:
        lines.append("- none")
    for item in record.patches:
        lines.append(
            f"- [{item.get('status', 'unknown')}] {item.get('path', 'unknown')} "
            f"review={item.get('review_status', 'UNREVIEWED')} :: {item.get('summary', '')}"
        )
    return "\n".join(lines) + "\n"


def render_patch_apply_markdown(report: PatchApplyReport) -> str:
    """渲染批量 patch apply 报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
    if not report.records:
        lines.append("- 暂无 patch 需要 apply")
    for record in report.records[:100]:
        status = "OK" if record.ok else "FAIL"
        lines.append(
            f"- [{status}] `{record.run_id}` decision={record.decision} "
            f"patches={record.patch_count} applied={record.applied_count} blocked={record.blocked_count} "
            f"rollback={record.rollback_performed}"
        )
        lines.append(f"  - {record.message}")
    return "\n".join(lines) + "\n"


def render_patch_apply_record_markdown(record: PatchApplyRecord) -> str:
    """渲染单个 patch apply 记录。"""

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
        "## Test Commands",
        "",
    ]
    if record.test_commands:
        lines.extend(f"- {item}" for item in record.test_commands)
    else:
        lines.append("- none")
    lines.extend(["", "## Patches", ""])
    if not record.patches:
        lines.append("- none")
    for item in record.patches:
        lines.append(
            f"- [{item.get('apply_status', 'UNKNOWN')}] {item.get('path', 'unknown')} "
            f"status={item.get('status', 'unknown')} review={item.get('review_status', 'UNREVIEWED')} :: {item.get('message', item.get('summary', ''))}"
        )
    return "\n".join(lines) + "\n"


def render_dispatch_markdown(report: DispatchReport) -> str:
    """渲染父代理调度器报告。"""

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
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
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
    return "\n".join(lines) + "\n"


def render_dispatch_watch_markdown(report: DispatchWatchReport) -> str:
    """渲染父代理 watch 模式报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Cycles", ""])
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
    """渲染父代理 planner 报告。"""

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
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Records", ""])
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


def _render_board_line(item: SubAgentBoardItem) -> str:
    """渲染看板的一行。"""

    flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
    goal = item.goal.replace("\n", " ")[:100]
    return (
        f"- `{item.id}` status={item.status} verify={item.verification_status} "
        f"channel={item.channel_status} "
        f"depth={item.depth} owner={item.owner or 'none'} final={item.final_owner or 'none'} "
        f"evidence={item.evidence_count} requests={item.open_request_count} "
        f"gaps={item.open_gap_count} flags={flags} :: {goal}"
    )

