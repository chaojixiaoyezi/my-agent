"""Patch review and approval workflow service.

Human version:
这个模块处理 patch 审核的工作流决策，不涉及实际文件写入。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..reports import PatchReviewRecord, PatchReviewReport
from ..utils import _new_id

if TYPE_CHECKING:
    from ..models import SubAgentTask


_VALID_PATCH_STATUSES = {"applied", "planned", "blocked"}


class PatchReviewService:
    """Handle patch review workflow decisions."""

    def __init__(self, manager):
        self.manager = manager

    def review_patches(
        self,
        run_ids=None,
        *,
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

        selected = self.manager._select_runs(run_ids)
        records = []
        for task in selected:
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._review_patch_task(
                    task,
                    output=output,
                    patches=patches,
                    apply=apply,
                    reviewer=reviewer,
                    note=note,
                )
            )
            if limit > 0 and len(records) >= limit:
                break

        summary = {"total": len(records)}
        for record in records:
            summary[record.decision] = summary.get(record.decision, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
        return PatchReviewReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_review_report(
        self,
        run_ids=None,
        *,
        apply=False,
        reviewer="parent",
        note="",
        limit=0,
    ) -> PatchReviewReport:
        """Write patch review report to disk."""

        from ..file_io import append_jsonl
        from .patch_renderer import render_patch_review_markdown

        report = self.review_patches(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        (self.manager.workspace / "subagent_patch_review_report.json").write_text(
            json.dumps(
                {
                    "generated_at": report.generated_at,
                    "dry_run": report.dry_run,
                    "summary": report.summary,
                    "records": [
                        {
                            "id": r.id,
                            "run_id": r.run_id,
                            "dry_run": r.dry_run,
                            "applied": r.applied,
                            "ok": r.ok,
                            "decision": r.decision,
                            "message": r.message,
                            "patch_count": r.patch_count,
                            "approved_count": r.approved_count,
                            "blocked_count": r.blocked_count,
                            "reviewer": r.reviewer,
                            "note": r.note,
                            "evidence_paths": r.evidence_paths,
                            "patches": r.patches,
                            "created_at": r.created_at,
                        }
                        for r in report.records
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_PATCH_REVIEW.md").write_text(
            render_patch_review_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_patch_review_record_files(record)
            self.manager._index_patch_review(record)
            if apply:
                self._append_patch_review_log(record)
        self.manager._index_report(
            "subagent_patch_review_report",
            "latest",
            "Subagent patch review report",
            report,
            event_type="subagent_patch_review_report_written",
        )
        return report

    def _review_patch_task(
        self,
        task: SubAgentTask,
        *,
        output: dict,
        patches: list[dict],
        apply: bool,
        reviewer: str,
        note: str,
    ) -> PatchReviewRecord:
        """Review patches for a single task."""

        now = time.time()
        patch_count = len(patches)
        blocked = [
            item
            for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item
            for item in patches
            if str(item.get("status", "")).lower() not in _VALID_PATCH_STATUSES
        ]
        applied_patches = [
            item for item in patches if str(item.get("status", "")).lower() == "applied"
        ]
        ok = bool(patches) and not blocked and not invalid
        decision = "APPROVE" if ok else "REJECT"
        if not patches:
            decision = "NO_PATCHES"
            message = "没有 patch 需要审核。"
        elif blocked or invalid:
            parts = []
            if blocked:
                parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
            if invalid:
                parts.append(f"{len(invalid)} 个 patch 状态未知")
            message = "；".join(parts) + "，不能审核通过。"
        else:
            message = f"{len(applied_patches)} 个 patch 已声明 applied，可审核通过。"

        applied = False
        reviewed_patches = [dict(item) for item in patches]
        if apply and patches:
            if ok:
                for item in reviewed_patches:
                    item["review_status"] = "APPROVED"
                    item["reviewed_by"] = reviewer
                    item["reviewed_at"] = now
                    if note:
                        item["review_note"] = note
                output["patches"] = reviewed_patches
                Path(task.output_json).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self.manager._append_task_work_log(
                    task,
                    f"patch_review: approved={len(reviewed_patches)} reviewer={reviewer}",
                )
                applied = True
            else:
                for item in reviewed_patches:
                    if str(item.get("status", "")).lower() != "applied":
                        item["review_status"] = "NEEDS_ACTION"
                        item["reviewed_by"] = reviewer
                        item["reviewed_at"] = now
                        if note:
                            item["review_note"] = note
                output["patches"] = reviewed_patches
                Path(task.output_json).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self.manager._append_task_work_log(
                    task,
                    f"patch_review: blocked={len(blocked) + len(invalid)} reviewer={reviewer}",
                )
                applied = True

        return PatchReviewRecord(
            id=_new_id("patchreview"),
            run_id=task.id,
            dry_run=not apply,
            applied=applied,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
            approved_count=len(reviewed_patches) if ok else 0,
            blocked_count=len(blocked) + len(invalid),
            reviewer=reviewer,
            note=note,
            evidence_paths=[task.output_json, task.work_log_file],
            patches=reviewed_patches,
            created_at=now,
        )

    def _write_patch_review_record_files(self, record: PatchReviewRecord) -> None:
        """Write single patch review record to task directory."""

        from .patch_renderer import render_patch_review_record_markdown

        try:
            task = self.manager.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "patch_review.json"
        record_md = Path(task.task_dir) / "PATCH_REVIEW.md"
        record_json.write_text(
            json.dumps(
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
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        record_md.write_text(render_patch_review_record_markdown(record), encoding="utf-8")

    def _append_patch_review_log(self, record: PatchReviewRecord) -> None:
        """Append patch review record to global audit log."""

        from ..file_io import append_jsonl

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
