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
    """Dispatch record, reporting, and watch loop service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def make_dispatch_record(
        self,
        *,
        step: str,
        action: str,
        run_id: str = "",
        dry_run: bool = True,
        applied: bool = False,
        ok: bool = True,
        message: str = "",
        before_status: str = "",
        after_status: str = "",
        before_verification_status: str = "",
        after_verification_status: str = "",
        evidence_paths: list[str] | None = None,
    ) -> DispatchRecord:
        """Create a dispatch audit record."""
        from ..reports import DispatchRecord
        from .dispatch_params import DispatchRecordParams

        params = DispatchRecordParams(
            step=step,
            action=action,
            run_id=run_id,
            dry_run=dry_run,
            applied=applied,
            ok=ok,
            message=message,
            before_status=before_status,
            after_status=after_status,
            before_verification_status=before_verification_status,
            after_verification_status=after_verification_status,
            evidence_paths=evidence_paths,
        )
        return self._make_dispatch_record(params)

    def _make_dispatch_record(self, params: DispatchRecordParams) -> DispatchRecord:
        """Internal: create a dispatch audit record from params bundle."""
        from .dispatch_record_builder import DispatchRecordBuilder

        return DispatchRecordBuilder.make_record(self.manager, params)

    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        """Summarize dispatch audit records."""
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
        """Write dispatch report and optional audit log."""
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
        cycle: int,
        dry_run: bool,
        ok: bool,
        message: str,
        dispatch_record_count: int,
        dispatch_summary: dict[str, int] | None = None,
        started_at: float = 0.0,
        ended_at: float = 0.0,
        evidence_paths: list[str] | None = None,
    ) -> DispatchWatchRecord:
        """Create a watch loop record."""
        from .dispatch_watch_builder import DispatchWatchBuilder

        return DispatchWatchBuilder.make_record(
            self.manager,
            cycle=cycle,
            dry_run=dry_run,
            ok=ok,
            message=message,
            dispatch_record_count=dispatch_record_count,
            dispatch_summary=dispatch_summary,
            started_at=started_at,
            ended_at=ended_at,
            evidence_paths=evidence_paths,
        )

    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        """Summarize watch loop records."""
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
        """Write watch mode report."""
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
        """Write watch heartbeat for external process monitoring."""
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
        """Write the most recent parent planner prompt/response."""
        prompt_path = self.manager.workspace / "parent_planner_prompt.md"
        response_path = self.manager.workspace / "parent_planner_response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        if response:
            response_path.write_text(response, encoding="utf-8")
        return str(prompt_path), str(response_path)

    def make_parent_planner_record(
        self,
        *,
        dry_run: bool,
        triggered: bool,
        ok: bool,
        decision: str,
        message: str,
        gate_summary: dict[str, int] | None = None,
        backend: str = "",
        tool_rounds: int = 0,
        parse_error: str = "",
        summary: str = "",
        actions: list[dict[str, object]] | None = None,
        blockers: list[str] | None = None,
        risks: list[str] | None = None,
        notes: list[str] | None = None,
        runner_instruction: str = "",
        suggested_max_runners: int = 0,
        prompt_path: str = "",
        response_path: str = "",
        evidence_paths: list[str] | None = None,
    ) -> ParentPlannerRecord:
        """Create a parent planner audit record."""
        from .parent_planner_builder import ParentPlannerBuilder

        return ParentPlannerBuilder.make_record(
            self.manager,
            dry_run=dry_run,
            triggered=triggered,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=gate_summary,
            backend=backend,
            tool_rounds=tool_rounds,
            parse_error=parse_error,
            summary=summary,
            actions=actions,
            blockers=blockers,
            risks=risks,
            notes=notes,
            runner_instruction=runner_instruction,
            suggested_max_runners=suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=evidence_paths,
        )

    def build_parent_planner_report(
        self,
        records: list[ParentPlannerRecord],
        *,
        dry_run: bool,
    ) -> ParentPlannerReport:
        """Summarize parent planner records."""
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
        """Write parent planner report and optional audit log."""
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
        """Write global watch audit log."""
        from .dispatch_watch_log_appender import DispatchWatchLogAppender

        DispatchWatchLogAppender.append(record, self.manager.workspace, self.manager)

    def append_parent_planner_log(self, record: ParentPlannerRecord) -> None:
        """Write global parent planner audit log."""
        from .parent_planner_builder import ParentPlannerLogAppender

        ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)