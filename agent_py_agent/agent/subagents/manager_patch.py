from __future__ import annotations

"""LLM contract: SubAgentPatchMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import *
from .reports import *
from .rendering import *
from .runner_rendering import *
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentPatchMixin:
    def review_patches(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchReviewReport:
        """审核 runner 输出里的 patch 记录。

        这里不直接应用任意 patch，只审核 runner 已声明的 patch 状态。
        `planned` / `blocked` patch 会被拦住，防止未处理改动进入 DONE。
        """

        selected = self._select_runs(run_ids)
        records: list[PatchReviewRecord] = []
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

        summary: dict[str, int] = {"total": len(records)}
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

    def write_patch_review_report(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchReviewReport:
        """写出 patch 审核报告。"""

        report = self.review_patches(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        (self.workspace / "subagent_patch_review_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_PATCH_REVIEW.md").write_text(
            render_patch_review_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_patch_review_record_files(record)
            self._index_patch_review(record)
            if apply:
                self._append_patch_review_log(record)
        self._index_report(
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
        output: dict[str, object],
        patches: list[dict[str, object]],
        apply: bool,
        reviewer: str,
        note: str,
    ) -> PatchReviewRecord:
        """审核单个任务的 patch 输出。"""

        now = time.time()
        patch_count = len(patches)
        valid_patch_statuses = {"applied", "planned", "blocked"}
        blocked = [
            item
            for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item
            for item in patches
            if str(item.get("status", "")).lower() not in valid_patch_statuses
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
                self._append_task_work_log(
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
                self._append_task_work_log(
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
        """把单个 patch 审核记录写入对应任务目录。"""

        try:
            task = self.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "patch_review.json"
        record_md = Path(task.task_dir) / "PATCH_REVIEW.md"
        record_json.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_patch_review_record_markdown(record), encoding="utf-8")

    def _append_patch_review_log(self, record: PatchReviewRecord) -> None:
        """写入全局 patch 审核日志。"""

        jsonl = self.workspace / "subagent_patch_review_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "PATCH_REVIEW_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PATCH REVIEW LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"applied={record.applied} message={record.message}\n"
            )
        self._index_patch_review(record)

