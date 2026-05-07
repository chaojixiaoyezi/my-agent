from __future__ import annotations

"""LLM contract: SubAgentAcceptanceMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .acceptance_review_service import (
    AcceptanceReviewOptions,
    AcceptanceReviewRequest,
    acceptance_review_options,
    review_acceptance_task,
)
from .models import SubAgentTask
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
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
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
from .rendering import render_acceptance_record_markdown, render_acceptance_review_markdown
from .reports import AcceptanceReviewRecord, AcceptanceReviewReport
from .runner_rendering import _render_runner_item_line
from .services.indexing_params import IndexReportParams
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore

class _SubAgentAcceptanceFacade:
    def review_acceptance(
        self,
        run_id: str,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> AcceptanceReviewRecord:

        task = self.load(run_id)
        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )
        return self._review_acceptance_task(
            task,
            apply=opts.apply,
            reviewer=opts.reviewer,
            note=opts.note,
        )

    def review_acceptances(
        self,
        run_ids: list[str] | None = None,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:
        """批量验收子代理运行。

        不指定 run_id 时，只挑出正在等待验收的运行，避免误动历史任务。
        """

        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        selected = _selected_acceptance_runs(self, run_ids, opts.limit)
        records = [
            self._review_acceptance_task(
                task,
                apply=opts.apply,
                reviewer=opts.reviewer,
                note=opts.note,
            )
            for task in selected
        ]
        return AcceptanceReviewReport(
            generated_at=time.time(),
            dry_run=not opts.apply,
            summary=_acceptance_report_summary(records),
            records=records,
        )

    def write_acceptance_review_report(
        self,
        run_ids: list[str] | None = None,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:

        report = self.review_acceptances(
            run_ids,
            options=options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        (self.workspace / "subagent_acceptance_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACCEPTANCE.md").write_text(
            render_acceptance_review_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_acceptance_record_files(record)
            self._index_acceptance_review(record)
            if report.dry_run is False:
                self._append_acceptance_review_log(record)
        self._index_report(
            IndexReportParams(
                "subagent_acceptance_report",
                "latest",
                "Subagent acceptance report",
                report,
                "subagent_acceptance_report_written",
            ),
        )
        return report

    def _review_acceptance_task(
        self,
        task: SubAgentTask,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> AcceptanceReviewRecord:
        # LLM: manager keeps the legacy method shape but service receives one request bundle.
        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )
        return review_acceptance_task(
            self,
            AcceptanceReviewRequest(
                task=task,
                apply=opts.apply,
                reviewer=opts.reviewer,
                note=opts.note,
                now=opts.now,
            ),
        )

    def _write_acceptance_record_files(self, record: AcceptanceReviewRecord) -> None:

        try:
            task = self.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "acceptance_review.json"
        record_md = Path(task.task_dir) / "ACCEPTANCE_REVIEW.md"
        record_json.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_acceptance_record_markdown(record), encoding="utf-8")
        with Path(task.acceptance_file).open("a", encoding="utf-8") as handle:
            handle.write("\n## Review\n\n")
            handle.write(f"- id: {record.id}\n")
            handle.write(f"- decision: {record.decision}\n")
            handle.write(f"- ok: {record.ok}\n")
            handle.write(f"- applied: {record.applied}\n")
            handle.write(f"- message: {record.message}\n")

    def _append_acceptance_review_log(self, record: AcceptanceReviewRecord) -> None:

        jsonl = self.workspace / "subagent_acceptance_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "ACCEPTANCE_REVIEW_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACCEPTANCE REVIEW LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"applied={record.applied} message={record.message}\n"
            )
        self._index_acceptance_review(record)


class SubAgentAcceptanceMixin(_SubAgentAcceptanceFacade):
    """Public compatibility mixin; implementation lives in the internal facade."""


def _selected_acceptance_runs(manager, run_ids: list[str] | None, limit: int):
    selected = manager._select_runs(run_ids)
    if run_ids is None:
        selected = [
            task
            for task in selected
            if task.status == "AWAITING_ACCEPTANCE"
            or task.verification_status == "NEEDS_ACCEPTANCE"
        ]
    return selected[:limit] if limit > 0 else selected


def _acceptance_report_summary(records: list[AcceptanceReviewRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary
