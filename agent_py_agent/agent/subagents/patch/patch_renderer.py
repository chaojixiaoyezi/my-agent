# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch review and apply diff rendering.

Human version:
这个模块只负责把 patch 记录渲染成人类可读的格式。
不涉及任何业务决策或文件写入。
"""

from __future__ import annotations

import difflib

from ..reports import PatchApplyRecord, PatchApplyReport, PatchReviewRecord, PatchReviewReport


# LLM: render_patch_review_markdown 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 渲染或汇总补丁审查markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def render_patch_review_markdown(report: PatchReviewReport) -> str:
    """Render batch patch review report to markdown."""

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


# LLM: render_patch_review_record_markdown 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 渲染或汇总补丁审查记录markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def render_patch_review_record_markdown(record: PatchReviewRecord) -> str:
    """Render single patch review record to markdown."""

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


# LLM: render_patch_apply_markdown 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 渲染或汇总补丁应用markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新补丁文件、预演结果和应用报告，需避免破坏既有状态机约定。
def render_patch_apply_markdown(report: PatchApplyReport) -> str:
    """Render batch patch apply report to markdown."""

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


# LLM: render_patch_apply_record_markdown 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 渲染或汇总补丁应用记录markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动补丁文件、预演结果和应用报告，调用方依赖写入顺序和文件格式。
def render_patch_apply_record_markdown(record: PatchApplyRecord) -> str:
    """Render single patch apply record to markdown."""

    lines = [
        "# PATCH APPLY",
        "",
        f"- id: {record.id}",
        f"- run_id: {record.run_id}",
        f"- mode: {'dry-run' if record.dry_run else 'apply'}",
        f"- decision: {record.decision}",
        f"- ok: {record.ok}",
        f"- applied: {record.applied}",
        f"- rollback: {record.rollback_performed}",
        f"- applier: {record.applier or 'none'}",
        f"- note: {record.note or 'none'}",
        f"- patch_count: {record.patch_count}",
        f"- applied_count: {record.applied_count}",
        f"- blocked_count: {record.blocked_count}",
        f"- message: {record.message}",
        "",
        "## Test Commands",
        "",
    ]
    if not record.test_commands:
        lines.append("- none")
    else:
        for cmd in record.test_commands:
            lines.append(f"- `{cmd}`")
    lines.extend(["", "## Test Results", ""])
    if not record.test_results:
        lines.append("- none")
    else:
        for result in record.test_results:
            status = "OK" if result.get("ok") else "FAIL"
            lines.append(
                f"- [{status}] returncode={result.get('returncode')} command={result.get('command')}"
            )
    lines.extend(["", "## Patches", ""])
    if not record.patches:
        lines.append("- none")
    for item in record.patches:
        lines.append(
            f"- [{item.get('status', 'unknown')}] {item.get('path', 'unknown')} "
            f"apply_status={item.get('apply_status', 'PENDING')} :: {item.get('message', '')}"
        )
    return "\n".join(lines) + "\n"


# LLM: build_unified_diff 属于子代理补丁应用的函数边界；调整时先确认补丁文件、预演结果和应用报告仍按原契约工作。
# 函数用途: 构建unifieddiff所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_unified_diff(path: str, before_text: str, after_text: str) -> str:
    """Build unified diff between two text strings."""

    lines = list(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return "".join(lines)
