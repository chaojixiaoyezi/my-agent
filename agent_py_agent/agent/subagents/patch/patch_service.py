"""Patch review and approval workflow service.

Human version:
这个模块处理 patch 审核的工作流决策，不涉及实际文件写入。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..reports import PatchReviewRecord, PatchReviewReport
from ..utils import _new_id

if TYPE_CHECKING:
    from ..models import SubAgentTask

_VALID_PATCH_STATUSES = {"applied", "planned", "blocked"}


@dataclass(frozen=True)
class PatchReviewOptions:
    """Options bundle for patch review report entrypoints."""

    # LLM: review policy options stay grouped while legacy fields remain thin adapters.
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    limit: int = 0

    @classmethod
    def from_values(
        cls,
        options: PatchReviewOptions | None = None,
        *,
        apply: bool | None = None,
        reviewer: str | None = None,
        note: str | None = None,
        limit: int | None = None,
    ):
        base = options or cls()
        updates = {"apply": apply, "reviewer": reviewer, "note": note, "limit": limit}
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)


@dataclass(frozen=True)
class PatchReviewTaskRequest:
    """Request bundle for reviewing one task's patches."""

    # LLM: per-task review state is passed as one request to avoid partial call-site drift.
    task: SubAgentTask
    output: dict
    patches: list[dict]
    options: PatchReviewOptions


def _patch_review_options(
    options: PatchReviewOptions | None,
    *,
    apply: bool,
    reviewer: str,
    note: str,
    limit: int,
) -> PatchReviewOptions:
    if options is not None and (apply, reviewer, note, limit) == (False, "parent", "", 0):
        return options
    return PatchReviewOptions.from_values(
        options,
        apply=apply,
        reviewer=reviewer,
        note=note,
        limit=limit,
    )


def _categorize_patches(patches: list[dict]) -> tuple[list, list, list]:
    blocked = [item for item in patches if str(item.get("status", "")).lower() in {"planned", "blocked"}]
    invalid = [item for item in patches if str(item.get("status", "")).lower() not in _VALID_PATCH_STATUSES]
    applied = [item for item in patches if str(item.get("status", "")).lower() == "applied"]
    return blocked, invalid, applied

def _build_review_message(patches: list[dict], blocked: list, invalid: list, applied: list) -> tuple[str, str]:
    if not patches:
        return "NO_PATCHES", "没有 patch 需要审核。"
    ok = not blocked and not invalid
    if blocked or invalid:
        parts = []
        if blocked:
            parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
        if invalid:
            parts.append(f"{len(invalid)} 个 patch 状态未知")
        return "REJECT" if not ok else "APPROVE", "; ".join(parts) + "，不能审核通过。"
    return "APPROVE" if ok else "REJECT", f"{len(applied)} 个 patch 已声明 applied，可审核通过。"

def _serialize_patch_review_record(record: PatchReviewRecord) -> dict:
    return {
        "id": record.id, "run_id": record.run_id, "dry_run": record.dry_run,
        "applied": record.applied, "ok": record.ok, "decision": record.decision,
        "message": record.message, "patch_count": record.patch_count,
        "approved_count": record.approved_count, "blocked_count": record.blocked_count,
        "reviewer": record.reviewer, "note": record.note,
        "evidence_paths": record.evidence_paths, "patches": record.patches,
        "created_at": record.created_at,
    }

def _serialize_patch_review_report(report: PatchReviewReport) -> dict:
    return {
        "generated_at": report.generated_at, "dry_run": report.dry_run,
        "summary": report.summary,
        "records": [_serialize_patch_review_record(r) for r in report.records],
    }

class PatchReviewService:

    def __init__(self, manager):
        self.manager = manager

    def review_patches(
        self,
        run_ids=None,
        *,
        options: PatchReviewOptions | None = None,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ) -> PatchReviewReport:
        """Review runner output patch records.

        Blocks `planned` / `blocked` patches to prevent unhandled changes from entering DONE.
        """

        from ..parsing import _dict_list
        from ..utils import _read_json_object

        opts = _patch_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        selected = self.manager._select_runs(run_ids)
        records = []
        for task in selected:
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._review_patch_task(
                    PatchReviewTaskRequest(
                        task=task,
                        output=output,
                        patches=patches,
                        options=opts,
                    )
                )
            )
            if opts.limit > 0 and len(records) >= opts.limit:
                break

        return PatchReviewReport(
            generated_at=time.time(),
            dry_run=not opts.apply,
            summary=_patch_review_summary(records),
            records=records,
        )

    def write_review_report(
        self,
        run_ids=None,
        *,
        options: PatchReviewOptions | None = None,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ) -> PatchReviewReport:
        from .patch_renderer import render_patch_review_markdown

        opts = _patch_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        report = self.review_patches(run_ids, options=opts)
        self._write_report_json(report)
        (self.manager.workspace / "SUBAGENT_PATCH_REVIEW.md").write_text(
            render_patch_review_markdown(report), encoding="utf-8",
        )
        for record in report.records:
            self._write_patch_review_record_files(record)
            self.manager._index_patch_review(record)
            if opts.apply:
                self._append_patch_review_log(record)
        self.manager._index_report("subagent_patch_review_report", "latest", "Subagent patch review report", report, event_type="subagent_patch_review_report_written")
        return report

    def _write_report_json(self, report: PatchReviewReport) -> None:
        (self.manager.workspace / "subagent_patch_review_report.json").write_text(
            json.dumps(_serialize_patch_review_report(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _review_patch_task(
        self,
        task: SubAgentTask | PatchReviewTaskRequest,
        *,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> PatchReviewRecord:
        if isinstance(task, PatchReviewTaskRequest):
            request = task
            task = request.task
            output = request.output
            patches = request.patches
            opts = request.options
        else:
            opts = PatchReviewOptions(apply=apply, reviewer=reviewer, note=note)
            output = output or {}
            patches = patches or []

        now = time.time()
        blocked, invalid, applied_patches = _categorize_patches(patches)
        ok = bool(patches) and not blocked and not invalid
        decision, message = _build_review_message(patches, blocked, invalid, applied_patches)
        reviewed_patches = [dict(item) for item in patches]

        if opts.apply and patches:
            self._apply_review_status(reviewed_patches, ok, opts.reviewer, now, opts.note)
            output["patches"] = reviewed_patches
            Path(task.output_json).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            self.manager._append_task_work_log(task, f"patch_review: {'approved' if ok else 'blocked'}={len(reviewed_patches)} reviewer={opts.reviewer}")

        return PatchReviewRecord(
            id=_new_id("patchreview"), run_id=task.id, dry_run=not opts.apply,
            applied=opts.apply and bool(patches), ok=ok, decision=decision, message=message,
            patch_count=len(patches), approved_count=len(reviewed_patches) if ok else 0,
            blocked_count=len(blocked) + len(invalid), reviewer=opts.reviewer, note=opts.note,
            evidence_paths=[task.output_json, task.work_log_file], patches=reviewed_patches, created_at=now,
        )

    def _apply_review_status(self, patches: list[dict], ok: bool, reviewer: str, now: float, note: str) -> None:
        if ok:
            self._apply_approved_patches(patches, reviewer, now, note)
        else:
            self._apply_rejected_patches(patches, reviewer, now, note)

    def _write_patch_review_record_files(self, record: PatchReviewRecord) -> None:
        from .patch_renderer import render_patch_review_record_markdown

        try:
            task = self.manager.load(record.run_id)
        except FileNotFoundError:
            return
        (Path(task.reports_dir) / "patch_review.json").write_text(
            json.dumps(_serialize_patch_review_record(record), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (Path(task.task_dir) / "PATCH_REVIEW.md").write_text(
            render_patch_review_record_markdown(record), encoding="utf-8",
        )

    def _apply_approved_patches(self, patches: list[dict], reviewer: str, now: float, note: str) -> None:
        for item in patches:
            item["review_status"] = "APPROVED"
            item["reviewed_by"] = reviewer
            item["reviewed_at"] = now
            if note:
                item["review_note"] = note

    def _apply_rejected_patches(self, patches: list[dict], reviewer: str, now: float, note: str) -> None:
        for item in patches:
            _apply_rejected_patch_status(item, reviewer=reviewer, now=now, note=note)

    def _append_patch_review_log(self, record: PatchReviewRecord) -> None:

        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "subagent_patch_review_log.jsonl"
        append_jsonl(
            jsonl,
            {
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
                "created_at": record.created_at,
            },
        )

        markdown = self.manager.workspace / "PATCH_REVIEW_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PATCH REVIEW LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"applied={record.applied} message={record.message}\n"
            )
        self.manager._index_patch_review(record)


def _patch_review_summary(records: list[PatchReviewRecord]) -> dict[str, int]:
    summary = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary


def _apply_rejected_patch_status(
    item: dict,
    *,
    reviewer: str,
    now: float,
    note: str,
) -> None:
    if str(item.get("status", "")).lower() == "applied":
        return
    item["review_status"] = "NEEDS_ACTION"
    item["reviewed_by"] = reviewer
    item["reviewed_at"] = now
    if note:
        item["review_note"] = note
