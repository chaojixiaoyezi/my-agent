# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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
    AcceptanceReviewFinding,
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    ActionApplyReport,
    ActionPlanReport,
    CapabilityRouteReport,
    DueCheckReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


# LLM: _summary_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总lines的展示文本，保持命令行、日志和审计输出一致；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _summary_lines(summary: dict[str, int]) -> list[str]:
    return [f"- {key}: {summary[key]}" for key in sorted(summary)]


# LLM: render_board_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总看板markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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


# LLM: _board_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理看板lines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _board_lines(items: list[SubAgentBoardItem], *, empty: str) -> list[str]:
    return [_render_board_line(item) for item in items] if items else [empty]


# LLM: render_due_check_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总到期检查markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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


# LLM: render_action_plan_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总动作计划markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
            # LLM: render rescue metadata next to the action that would use it.
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


# LLM: render_action_apply_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总动作应用markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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


# LLM: render_capability_route_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总能力routemarkdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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


# LLM: render_acceptance_review_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总验收审查markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def render_acceptance_review_markdown(report: AcceptanceReviewReport) -> str:
    mode = "dry-run" if report.dry_run else "apply"
    # LLM: acceptance reports separate worker claims from evidence and parent decisions.
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
        if record.worker_claims:
            lines.append(f"  - worker: {record.worker_claims[0]}")
        if record.evidence_facts:
            lines.append(f"  - evidence: {'; '.join(record.evidence_facts[:3])}")
        _extend_failed_acceptance_items(lines, record)
    return "\n".join(lines) + "\n"


# LLM: _extend_failed_acceptance_items 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理extendfailed验收条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _extend_failed_acceptance_items(
    lines: list[str],
    record: AcceptanceReviewRecord,
) -> None:
    # LLM: 审查摘要只展示阻塞性失败，完整细节仍留在记录分区。
    failed = [item for item in record.findings if not item.ok and item.severity != "P2"]
    failed.extend(item for item in record.verifier_checks if not item.ok and item.severity != "P2")
    for item in failed[:5]:
        lines.append(f"  - [{item.severity}] {item.name}: {item.message}")


# LLM: render_acceptance_record_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总验收记录markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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
        "## Worker Claims",
        "",
        *_list_or_none(record.worker_claims),
        "",
        "## Evidence Facts",
        "",
        *_list_or_none(record.evidence_facts),
        "",
        "## Parent Conclusions",
        "",
        *_list_or_none(record.parent_conclusions),
        "",
        "## Verifier Checks",
        "",
    ]
    _extend_acceptance_review_items(lines, record.verifier_checks)
    lines.extend(["## Findings", ""])
    _extend_acceptance_review_items(lines, record.findings)
    return "\n".join(lines) + "\n"


# LLM: _extend_acceptance_review_items 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理extend验收审查条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _extend_acceptance_review_items(
    lines: list[str],
    items: list[AcceptanceReviewFinding],
) -> None:
    # LLM: 验收记录分区共用渲染逻辑，但判定规则仍保留在调用方。
    if not items:
        lines.append("- none")
        return
    for item in items:
        status = "OK" if item.ok else "FAIL"
        lines.append(f"- [{status}] {item.severity} {item.name}: {item.message}")
        if item.evidence_path:
            lines.append(f"  - evidence: {item.evidence_path}")


# LLM: _list_or_none 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 读取或查询none需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _list_or_none(items: list[str]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {item}" for item in items]


# LLM: _render_board_line 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总看板line的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_board_line(item: SubAgentBoardItem) -> str:
    flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
    children = ",".join(f"{key}:{value}" for key, value in sorted(item.child_status_counts.items())) or "none"
    goal = item.goal.replace("\n", " ")[:100]
    # LLM: compact board lines surface task-tree status for parent triage.
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
