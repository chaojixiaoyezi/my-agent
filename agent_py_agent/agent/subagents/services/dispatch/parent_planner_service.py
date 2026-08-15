from __future__ import annotations

"""Parent planner report service.

这个服务只负责父代理 planner 的 prompt/response 记录和报告写出。
普通 dispatch/watch 报告留在 dispatch service，避免一个 service 承担两类账本。
"""

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ..indexing.records import IndexReportParams
from .params import ParentPlannerRecordParams

if TYPE_CHECKING:
    from ...reports import ParentPlannerRecord, ParentPlannerReport


class SubAgentParentPlannerService:
    def __init__(self, manager: Any):
        self.manager = manager

    def write_parent_planner_exchange(self, prompt: str, response: str = "") -> tuple[str, str]:
        prompt_path = self.manager.workspace / "parent_planner_prompt.md"
        response_path = self.manager.workspace / "parent_planner_response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        if response:
            response_path.write_text(response, encoding="utf-8")
        return str(prompt_path), str(response_path)

    def make_parent_planner_record(
        self,
        *,
        params: ParentPlannerRecordParams,
    ) -> ParentPlannerRecord:
        from ..parent_planner_builder import ParentPlannerBuilder

        return ParentPlannerBuilder.make_record(self.manager, params=params)

    def build_parent_planner_report(
        self,
        records: list[ParentPlannerRecord],
        *,
        dry_run: bool,
    ) -> ParentPlannerReport:
        from ...reports import ParentPlannerReport
        from ..parent_planner_builder import ParentPlannerBuilder

        summary = ParentPlannerBuilder.build_summary(records)
        return ParentPlannerReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_parent_planner_report(
        self,
        report: ParentPlannerReport,
        *,
        append_log: bool = False,
    ) -> ParentPlannerReport:
        from ...rendering import render_parent_planner_markdown

        (self.manager.workspace / "parent_planner_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "PARENT_PLANNER.md").write_text(
            render_parent_planner_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from ..parent_planner_builder import ParentPlannerLogAppender

            for record in report.records:
                ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)
        for record in report.records:
            self.manager.indexing.index_parent_planner_record(record)
        self.manager.indexing.index_report(
            IndexReportParams(
                "parent_planner_report", "latest",
                "Parent planner report", report,
                "parent_planner_report_written",
            ),
        )
        return report

    def append_parent_planner_log(self, record: ParentPlannerRecord) -> None:
        from ..parent_planner_builder import ParentPlannerLogAppender

        ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)
