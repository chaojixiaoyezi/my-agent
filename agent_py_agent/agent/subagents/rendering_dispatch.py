# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""markdown renderers for dispatch, watch, and parent planner reports.

给人看的解释：
这些报告都属于父代理调度视角，单独拆出后 rendering.py 保持兼容入口。
"""

from .reports import DispatchReport, DispatchWatchReport, ParentPlannerReport


# LLM: render_dispatch_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总markdown的展示文本，并展示 auto-policy refs、manual-only 与 preflight 摘要；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
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
        if record.parent_acceptance_policy_ref:
            lines.append(
                "  - parent_acceptance_auto_policy: "
                f"decision={record.parent_acceptance_policy_decision} "
                f"action={record.parent_acceptance_policy_action} "
                f"would_execute={record.parent_acceptance_policy_would_execute} "
                f"executed={record.parent_acceptance_policy_executed} "
                f"execution_mode={record.parent_acceptance_policy_execution_mode} "
                "automatic_execution_allowed="
                f"{record.parent_acceptance_policy_automatic_execution_allowed} "
                f"recommended_command={record.parent_acceptance_policy_recommended_command} "
                f"preflight_status={record.parent_acceptance_policy_preflight_status} "
                "ready_for_automatic_execution="
                f"{record.parent_acceptance_policy_ready_for_automatic_execution} "
                "preflight_blockers="
                f"{','.join(record.parent_acceptance_policy_preflight_blockers)} "
                f"ref={record.parent_acceptance_policy_ref}"
            )
    return "\n".join(lines) + "\n"


# LLM: render_dispatch_watch_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
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


# LLM: render_parent_planner_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总父级规划器markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
