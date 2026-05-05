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
from .acceptance_review_service import review_acceptance_task
from .runner_rendering import _render_runner_item_line
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

class SubAgentAcceptanceMixin:
    def review_acceptance(
        self,
        run_id: str,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> AcceptanceReviewRecord:
        """验收单个等待验收的子代理运行。"""

        task = self.load(run_id)
        return self._review_acceptance_task(
            task,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )

    def review_acceptances(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:
        """批量验收子代理运行。

        不指定 run_id 时，只挑出正在等待验收的运行，避免误动历史任务。
        """

        selected = self._select_runs(run_ids)
        if run_ids is None:
            selected = [
                task
                for task in selected
                if task.status == "AWAITING_ACCEPTANCE"
                or task.verification_status == "NEEDS_ACCEPTANCE"
            ]
        if limit > 0:
            selected = selected[:limit]

        records = [
            self._review_acceptance_task(
                task,
                apply=apply,
                reviewer=reviewer,
                note=note,
            )
            for task in selected
        ]
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
        return AcceptanceReviewReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_acceptance_review_report(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:
        """写出验收报告，并在 apply 时写回任务状态。"""

        report = self.review_acceptances(
            run_ids,
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
            if apply:
                self._append_acceptance_review_log(record)
        self._index_report(
            "subagent_acceptance_report",
            "latest",
            "Subagent acceptance report",
            report,
            event_type="subagent_acceptance_report_written",
        )
        return report

    def _review_acceptance_task(
        self,
        task: SubAgentTask,
        *,
        apply: bool,
        reviewer: str,
        note: str,
    ) -> AcceptanceReviewRecord:
        """对单个任务执行验收判断，并按需写回状态。"""
        return review_acceptance_task(self, task, apply=apply, reviewer=reviewer, note=note)

    def _write_acceptance_record_files(self, record: AcceptanceReviewRecord) -> None:
        """把单个验收记录写进对应任务目录。"""

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
        """写入全局验收审计日志。"""

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
