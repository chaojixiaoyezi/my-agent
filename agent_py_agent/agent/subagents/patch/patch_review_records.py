
from __future__ import annotations

"""Patch review serialization, status updates, and log persistence."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import PatchReviewRecord, PatchReviewReport


@dataclass(frozen=True)
class PatchReviewStatusUpdate:
    patches: list[dict]
    ok: bool
    reviewer: str
    now: float
    note: str


def serialize_patch_review_record(record: PatchReviewRecord) -> dict:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "dry_run": record.dry_run,
        "applied": record.applied,
        "ok": record.ok,
        "decision": record.decision,
        "message": record.message,
        "patch_count": record.patch_count,
        "approved_count": record.approved_count,
        "blocked_count": record.blocked_count,
        "reviewer": record.reviewer,
        "note": record.note,
        "evidence_paths": record.evidence_paths,
        "patches": record.patches,
        "load_errors": record.load_errors,
        "created_at": record.created_at,
    }


def serialize_patch_review_report(report: PatchReviewReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [serialize_patch_review_record(record) for record in report.records],
    }


# LLM: 复审报告 JSON 走私有写（0600/0700）；延迟导入避免模块级循环。
# 函数用途: 把补丁复审报告写成 JSON 文件。
def write_patch_review_report_json(manager, report: PatchReviewReport) -> None:
    from ...common.json_io import write_private_text_file_atomic

    # 补丁审阅报告属宿主运行数据：私有原子写（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
    write_private_text_file_atomic(
        manager.workspace / "subagent_patch_review_report.json",
        json.dumps(serialize_patch_review_report(report), ensure_ascii=False, indent=2),
    )


# LLM: 记录 JSON 与 Markdown 走私有写（0600/0700）；格式不变。
# 函数用途: 写出一条补丁复审记录的两个文件。
def write_patch_review_record_files(manager, record: PatchReviewRecord) -> None:
    from ...common.json_io import write_private_text_file_atomic
    from .patch_renderer import render_patch_review_record_markdown

    try:
        task = manager.load(record.run_id)
    except FileNotFoundError:
        return
    write_private_text_file_atomic(
        Path(task.reports_dir) / "patch_review.json",
        json.dumps(serialize_patch_review_record(record), ensure_ascii=False, indent=2),
    )
    write_private_text_file_atomic(
        Path(task.task_dir) / "PATCH_REVIEW.md",
        render_patch_review_record_markdown(record),
    )


def apply_review_status(update: PatchReviewStatusUpdate) -> None:
    if update.ok:
        _apply_approved_patches(update)
    else:
        _apply_rejected_patches(update)


# LLM: 日志追加走私有写（0600/0700）；JSONL 行格式不变。
# 函数用途: 把一条补丁复审记录追加进 JSONL 日志并补齐 Markdown 头。
def append_patch_review_log(manager, record: PatchReviewRecord) -> None:
    from ...common.json_io import (
        append_private_jsonl_records,
        append_private_text,
        write_private_text_file_atomic,
    )

    jsonl = manager.workspace / "subagent_patch_review_log.jsonl"
    # 补丁审阅账属宿主运行数据：私有追加（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(jsonl, [serialize_patch_review_record(record)], sort_keys=False)

    markdown = manager.workspace / "PATCH_REVIEW_LOG.md"
    if not markdown.exists():
        write_private_text_file_atomic(markdown, "# PATCH REVIEW LOG\n\n")
    status = "OK" if record.ok else "FAIL"
    append_private_text(
        markdown,
        f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
        f"applied={record.applied} message={record.message}\n",
    )
    manager.indexing.index_patch_review(record)


def _apply_approved_patches(update: PatchReviewStatusUpdate) -> None:
    for item in update.patches:
        item["review_status"] = "APPROVED"
        item["reviewed_by"] = update.reviewer
        item["reviewed_at"] = update.now
        if update.note:
            item["review_note"] = update.note


def _apply_rejected_patches(update: PatchReviewStatusUpdate) -> None:
    for item in update.patches:
        _apply_rejected_patch_status(item, reviewer=update.reviewer, now=update.now, note=update.note)


def _apply_rejected_patch_status(item: dict, *, reviewer: str, now: float, note: str) -> None:
    if str(item.get("status", "")) == "applied":
        return
    item["review_status"] = "NEEDS_ACTION"
    item["reviewed_by"] = reviewer
    item["reviewed_at"] = now
    if note:
        item["review_note"] = note
