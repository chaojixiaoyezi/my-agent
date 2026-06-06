
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


def write_patch_review_report_json(manager, report: PatchReviewReport) -> None:
    (manager.workspace / "subagent_patch_review_report.json").write_text(
        json.dumps(serialize_patch_review_report(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_patch_review_record_files(manager, record: PatchReviewRecord) -> None:
    from .patch_renderer import render_patch_review_record_markdown

    try:
        task = manager.load(record.run_id)
    except FileNotFoundError:
        return
    (Path(task.reports_dir) / "patch_review.json").write_text(
        json.dumps(serialize_patch_review_record(record), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (Path(task.task_dir) / "PATCH_REVIEW.md").write_text(
        render_patch_review_record_markdown(record),
        encoding="utf-8",
    )


def apply_review_status(update: PatchReviewStatusUpdate) -> None:
    if update.ok:
        _apply_approved_patches(update)
    else:
        _apply_rejected_patches(update)


def append_patch_review_log(manager, record: PatchReviewRecord) -> None:
    from ...io import append_jsonl

    jsonl = manager.workspace / "subagent_patch_review_log.jsonl"
    append_jsonl(jsonl, serialize_patch_review_record(record))

    markdown = manager.workspace / "PATCH_REVIEW_LOG.md"
    if not markdown.exists():
        markdown.write_text("# PATCH REVIEW LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        status = "OK" if record.ok else "FAIL"
        handle.write(
            f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
            f"applied={record.applied} message={record.message}\n"
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
    if str(item.get("status", "")).lower() == "applied":
        return
    item["review_status"] = "NEEDS_ACTION"
    item["reviewed_by"] = reviewer
    item["reviewed_at"] = now
    if note:
        item["review_note"] = note
