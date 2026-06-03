
from __future__ import annotations

"""Patch review/apply markdown renderers."""

from .reports import PatchApplyRecord, PatchApplyReport, PatchReviewRecord, PatchReviewReport


def _summary_lines(summary: dict[str, int]) -> list[str]:
    return [f"- {key}: {summary[key]}" for key in sorted(summary)]


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
