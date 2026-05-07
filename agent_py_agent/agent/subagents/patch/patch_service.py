"""Patch review and approval workflow service.

Human version:
这个模块处理 patch 审核的工作流决策，不涉及实际文件写入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..reports import PatchReviewRecord, PatchReviewReport
from ..services.indexing_params import IndexReportParams
from ..utils import _new_id
from .patch_review_records import (
    PatchReviewStatusUpdate,
    append_patch_review_log,
    apply_review_status,
    write_patch_review_record_files,
    write_patch_review_report_json,
)

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

        opts = _patch_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        records = _collect_patch_review_records(self, run_ids, opts)
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
        write_patch_review_report_json(self.manager, report)
        (self.manager.workspace / "SUBAGENT_PATCH_REVIEW.md").write_text(
            render_patch_review_markdown(report), encoding="utf-8",
        )
        for record in report.records:
            write_patch_review_record_files(self.manager, record)
            self.manager._index_patch_review(record)
            if opts.apply:
                append_patch_review_log(self.manager, record)
        self.manager._index_report(
            IndexReportParams(
                "subagent_patch_review_report",
                "latest",
                "Subagent patch review report",
                report,
                "subagent_patch_review_report_written",
            ),
        )
        return report

    def _review_patch_task(
        self,
        request: SubAgentTask | PatchReviewTaskRequest,
        *,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> PatchReviewRecord:
        task, output, patches, opts = _coerce_patch_review_request(
            request,
            output=output,
            patches=patches,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )

        now = time.time()
        blocked, invalid, applied_patches = _categorize_patches(patches)
        ok = bool(patches) and not blocked and not invalid
        decision, message = _build_review_message(patches, blocked, invalid, applied_patches)
        reviewed_patches = [dict(item) for item in patches]

        if opts.apply and patches:
            apply_review_status(PatchReviewStatusUpdate(reviewed_patches, ok, opts.reviewer, now, opts.note))
            output["patches"] = reviewed_patches
            import json

            Path(task.output_json).write_text(
                json.dumps(output, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.manager._append_task_work_log(task, f"patch_review: {'approved' if ok else 'blocked'}={len(reviewed_patches)} reviewer={opts.reviewer}")

        return PatchReviewRecord(
            id=_new_id("patchreview"), run_id=task.id, dry_run=not opts.apply,
            applied=opts.apply and bool(patches), ok=ok, decision=decision, message=message,
            patch_count=len(patches), approved_count=len(reviewed_patches) if ok else 0,
            blocked_count=len(blocked) + len(invalid), reviewer=opts.reviewer, note=opts.note,
            evidence_paths=[task.output_json, task.work_log_file], patches=reviewed_patches, created_at=now,
        )


def _collect_patch_review_records(service: PatchReviewService, run_ids, opts: PatchReviewOptions):
    from ..parsing import _dict_list
    from ..utils import _read_json_object

    records = []
    for task in service.manager._select_runs(run_ids):
        output = _read_json_object(Path(task.output_json))
        patches = _dict_list(output.get("patches", []))
        if run_ids is None and not patches:
            continue
        records.append(
            service._review_patch_task(
                PatchReviewTaskRequest(task=task, output=output, patches=patches, options=opts)
            )
        )
        if opts.limit > 0 and len(records) >= opts.limit:
            break
    return records


def _coerce_patch_review_request(
    request,
    *,
    output: dict | None,
    patches: list[dict] | None,
    apply: bool,
    reviewer: str,
    note: str,
):
    if isinstance(request, PatchReviewTaskRequest):
        return request.task, request.output, request.patches, request.options
    opts = PatchReviewOptions(apply=apply, reviewer=reviewer, note=note)
    return request, output or {}, patches or [], opts


def _patch_review_summary(records: list[PatchReviewRecord]) -> dict[str, int]:
    summary = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary
