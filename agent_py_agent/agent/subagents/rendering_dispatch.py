from __future__ import annotations

"""LLM: markdown renderers for dispatch, watch, and parent planner reports.

给人看的解释：
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
