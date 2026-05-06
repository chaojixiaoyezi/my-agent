from __future__ import annotations

"""LLM: dispatch record and reporting service.

给人看的解释：
这里承接调度记录生成、汇总和写出逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .dispatch_params import DispatchRecordParams
from .parent_planner_builder import ParentPlannerBuilder

if TYPE_CHECKING:
    from ..reports import (
        DispatchRecord,
        DispatchReport,
        DispatchWatchRecord,
        DispatchWatchReport,
        ParentPlannerRecord,
        ParentPlannerReport,
    )

class SubAgentDispatchService:

    def __init__(self, manager: Any):
        self.manager = manager

    def make_dispatch_record(
        self,
        *,
        params: DispatchRecordParams,
    ) -> DispatchRecord:
        return self._make_dispatch_record(params)

    def _make_dispatch_record(self, params: DispatchRecordParams) -> DispatchRecord:
        from .dispatch_record_builder import DispatchRecordBuilder

        return DispatchRecordBuilder.make_record(self.manager, params)

    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        from ..reports import DispatchReport
        from .dispatch_record_builder import DispatchRecordBuilder

        summary = DispatchRecordBuilder.build_summary(records)
        return DispatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_report(
        self,
        report: DispatchReport,
        *,
        append_log: bool = False,
    ) -> DispatchReport:
        (self.manager.workspace / "subagent_dispatch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH.md").write_text(
            self.manager._render_dispatch_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from .dispatch_log_appender import DispatchLogAppender

            for record in report.records:
                DispatchLogAppender.append(record, self.manager.workspace)
        for record in report.records:
            self.manager._index_dispatch_record(record)
        self.manager._index_report(
            "subagent_dispatch_report", "latest",
            "Subagent dispatch report", report,
            event_type="subagent_dispatch_report_written",
        )
        return report

    def make_dispatch_watch_record(
        self,
        *,
        params: DispatchWatchRecordParams,
    ) -> DispatchWatchRecord:
        from .dispatch_watch_builder import DispatchWatchBuilder

        return DispatchWatchBuilder.make_record(self.manager, params=params)

    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        from ..reports import DispatchWatchReport
        from .dispatch_watch_builder import DispatchWatchBuilder

        summary = DispatchWatchBuilder.build_summary(records)
        return DispatchWatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_watch_report(self, report: DispatchWatchReport) -> DispatchWatchReport:
        (self.manager.workspace / "subagent_dispatch_watch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH_WATCH.md").write_text(
            self.manager._render_dispatch_watch_markdown(report),
            encoding="utf-8",
        )
        self.manager._index_report(
            "subagent_dispatch_watch_report", "latest",
            "Subagent dispatch watch report", report,
            event_type="subagent_dispatch_watch_report_written",
        )
        return report

    def write_dispatch_watch_heartbeat(
        self,
        *,
        cycle: int,
        status: str,
        lock_path: str,
        pid: int,
        message: str = "",
    ) -> Path:
        path = self.manager.workspace / "subagent_dispatch_watch_heartbeat.json"
        payload = {
            "cycle": cycle,
            "status": status,
            "lock_path": lock_path,
            "pid": pid,
            "message": message,
            "updated_at": time.time(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

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
        from .parent_planner_builder import ParentPlannerBuilder

        return ParentPlannerBuilder.make_record(self.manager, params=params)

    def build_parent_planner_report(
        self,
        records: list[ParentPlannerRecord],
        *,
        dry_run: bool,
    ) -> ParentPlannerReport:
        from ..reports import ParentPlannerReport
        from .parent_planner_builder import ParentPlannerBuilder

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
        (self.manager.workspace / "parent_planner_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "PARENT_PLANNER.md").write_text(
            self.manager._render_parent_planner_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from .parent_planner_builder import ParentPlannerLogAppender

            for record in report.records:
                ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)
        for record in report.records:
            self.manager._index_parent_planner_record(record)
        self.manager._index_report(
            "parent_planner_report", "latest",
            "Parent planner report", report,
            event_type="parent_planner_report_written",
        )
        return report

    def append_dispatch_watch_log(self, record: DispatchWatchRecord) -> None:
        from .dispatch_watch_log_appender import DispatchWatchLogAppender

        DispatchWatchLogAppender.append(record, self.manager.workspace, self.manager)

    def append_parent_planner_log(self, record: ParentPlannerRecord) -> None:
        from .parent_planner_builder import ParentPlannerLogAppender

        ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)
