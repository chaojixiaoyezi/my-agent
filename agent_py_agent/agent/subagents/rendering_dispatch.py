
from __future__ import annotations

"""markdown renderers for dispatch, watch, and parent planner reports.

这些报告都属于父代理调度视角，单独拆出后 rendering.py 保持兼容入口。
"""

from .reports import DispatchReport, DispatchWatchReport, ParentPlannerReport


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


def _dispatch_completion_gate_lines(records: list[object]) -> list[str]:
    blockers = _dispatch_blocking_run_ids(records)
    lines = ["", "## Completion Gate", ""]
    if not blockers:
        lines.extend([
            "- status: complete_or_no_blockers",
            "- must_not_report_done: false",
            "- blocking_run_ids: (none)",
        ])
        return lines
    lines.extend([
        "- status: not_complete",
        "- must_not_report_done: true",
        f"- blocking_run_ids: {', '.join(blockers)}",
        "- next_action: repair_or_continue_blocking_run_ids before final user-facing completion.",
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
