# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Patch review/apply markdown renderers."""

from .reports import PatchApplyRecord, PatchApplyReport, PatchReviewRecord, PatchReviewReport


# LLM: _summary_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总lines的展示文本，保持命令行、日志和审计输出一致；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _summary_lines(summary: dict[str, int]) -> list[str]:
    return [f"- {key}: {summary[key]}" for key in sorted(summary)]


# LLM: render_patch_review_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总补丁审查markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
    return "\n".join(lines) + "\n"


# LLM: render_patch_review_record_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总补丁审查记录markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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
        "## Patches",
        "",
        *_patch_review_lines(record.patches),
    ]
    return "\n".join(lines) + "\n"


# LLM: _patch_review_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理补丁审查lines相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _patch_review_lines(patches: list[dict[str, object]]) -> list[str]:
    if not patches:
        return ["- none"]
    return [
        f"- [{item.get('status', 'unknown')}] {item.get('path', 'unknown')} "
        f"review={item.get('review_status', 'UNREVIEWED')} :: {item.get('summary', '')}"
        for item in patches
    ]


# LLM: render_patch_apply_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总补丁应用markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
    return "\n".join(lines) + "\n"


# LLM: render_patch_apply_record_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总补丁应用记录markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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
        "## Test Commands",
        "",
    ]
    lines.extend(f"- {item}" for item in record.test_commands or ["none"])
    lines.extend(["", "## Patches", "", *_patch_apply_lines(record.patches)])
    return "\n".join(lines) + "\n"


# LLM: _patch_apply_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理补丁应用lines相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _patch_apply_lines(patches: list[dict[str, object]]) -> list[str]:
    if not patches:
        return ["- none"]
    return [
        f"- [{item.get('apply_status', 'UNKNOWN')}] {item.get('path', 'unknown')} "
        f"status={item.get('status', 'unknown')} review={item.get('review_status', 'UNREVIEWED')} :: "
        f"{item.get('message', item.get('summary', ''))}"
        for item in patches
    ]
