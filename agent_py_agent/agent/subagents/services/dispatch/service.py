
from __future__ import annotations

"""dispatch record and reporting service.

这里承接调度记录生成、汇总和写出逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..indexing.params import IndexReportParams
from .params import (
    DispatchRecordParams,
    DispatchWatchHeartbeatParams,
    DispatchWatchRecordParams,
)

if TYPE_CHECKING:
    from ...reports import (
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
        params: DispatchRecordParams | None = None,
        step: str = "",
        action: str = "",
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
        params = params or _dispatch_record_params(
            locals(),
        )
        return self._make_dispatch_record(params)

    def _make_dispatch_record(self, params: DispatchRecordParams) -> DispatchRecord:
        from .record_builder import DispatchRecordBuilder

        return DispatchRecordBuilder.make_record(self.manager, params)

    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        from ...reports import DispatchReport
        from .record_builder import DispatchRecordBuilder

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
        from ...rendering import render_dispatch_markdown

        (self.manager.workspace / "subagent_dispatch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH.md").write_text(
            render_dispatch_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from .log_appender import DispatchLogAppender

            for record in report.records:
                DispatchLogAppender.append(record, self.manager.workspace)
        for record in report.records:
            self.manager._index_dispatch_record(record)
        self.manager._index_report(
            IndexReportParams(
                "subagent_dispatch_report", "latest",
                "Subagent dispatch report", report,
                "subagent_dispatch_report_written",
            ),
        )
        return _trace_written_dispatch_report(self.manager, report)

    def _append_dispatch_log(self, record: DispatchRecord) -> None:
        from .log_appender import DispatchLogAppender

        DispatchLogAppender.append(record, self.manager.workspace)

    def make_dispatch_watch_record(
        self,
        *,
        params: DispatchWatchRecordParams | None = None,
        cycle: int = 0,
        dry_run: bool = True,
        ok: bool = True,
        message: str = "",
        dispatch_record_count: int = 0,
        dispatch_summary: dict[str, int] | None = None,
        started_at: float = 0.0,
        ended_at: float = 0.0,
        evidence_paths: list[str] | None = None,
    ) -> DispatchWatchRecord:
        from .watch_builder import DispatchWatchBuilder

        params = params or _dispatch_watch_record_params(
            locals(),
        )
        return DispatchWatchBuilder.make_record(self.manager, params=params)

    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        from ...reports import DispatchWatchReport
        from .watch_builder import DispatchWatchBuilder

        summary = DispatchWatchBuilder.build_summary(records)
        return DispatchWatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_watch_report(self, report: DispatchWatchReport) -> DispatchWatchReport:
        from ...rendering import render_dispatch_watch_markdown

        (self.manager.workspace / "subagent_dispatch_watch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH_WATCH.md").write_text(
            render_dispatch_watch_markdown(report),
            encoding="utf-8",
        )
        self.manager._index_report(
            IndexReportParams(
                "subagent_dispatch_watch_report", "latest",
                "Subagent dispatch watch report", report,
                "subagent_dispatch_watch_report_written",
            ),
        )
        return _trace_written_dispatch_watch_report(self.manager, report)

    def write_dispatch_watch_heartbeat(
        self,
        *,
        params: DispatchWatchHeartbeatParams | None = None,
        cycle: int = 0,
        status: str = "",
        lock_path: str = "",
        pid: int = 0,
        message: str = "",
    ) -> Path:
        params = params or _dispatch_watch_heartbeat_params(
            locals(),
        )
        path = self.manager.workspace / "subagent_dispatch_watch_heartbeat.json"
        payload = {
            "cycle": params.cycle,
            "status": params.status,
            "lock_path": params.lock_path,
            "pid": params.pid,
            "message": params.message,
            "updated_at": time.time(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def append_dispatch_watch_log(self, record: DispatchWatchRecord) -> None:
        from .watch_log_appender import DispatchWatchLogAppender

        DispatchWatchLogAppender.append(record, self.manager.workspace, self.manager)


def _dispatch_record_params(values: dict[str, object]) -> DispatchRecordParams:
    evidence_paths = values.get("evidence_paths")
    return DispatchRecordParams(
        step=str(values.get("step") or ""),
        action=str(values.get("action") or ""),
        run_id=str(values.get("run_id") or ""),
        dry_run=bool(values.get("dry_run")),
        applied=bool(values.get("applied")),
        ok=bool(values.get("ok")),
        message=str(values.get("message") or ""),
        before_status=str(values.get("before_status") or ""),
        after_status=str(values.get("after_status") or ""),
        before_verification_status=str(values.get("before_verification_status") or ""),
        after_verification_status=str(values.get("after_verification_status") or ""),
        evidence_paths=evidence_paths if isinstance(evidence_paths, list) else None,
        collaboration_candidate_load_errors=(
            values.get("collaboration_candidate_load_errors")
            if isinstance(values.get("collaboration_candidate_load_errors"), list)
            else None
        ),
    )


def _dispatch_watch_record_params(values: dict[str, object]) -> DispatchWatchRecordParams:
    dispatch_summary = values.get("dispatch_summary")
    evidence_paths = values.get("evidence_paths")
    return DispatchWatchRecordParams(
        cycle=int(values.get("cycle") or 0),
        dry_run=bool(values.get("dry_run")),
        ok=bool(values.get("ok")),
        message=str(values.get("message") or ""),
        dispatch_record_count=int(values.get("dispatch_record_count") or 0),
        dispatch_summary=dispatch_summary if isinstance(dispatch_summary, dict) else None,
        started_at=float(values.get("started_at") or 0.0),
        ended_at=float(values.get("ended_at") or 0.0),
        evidence_paths=evidence_paths if isinstance(evidence_paths, list) else None,
    )


def _dispatch_watch_heartbeat_params(values: dict[str, object]) -> DispatchWatchHeartbeatParams:
    return DispatchWatchHeartbeatParams(
        cycle=int(values.get("cycle") or 0),
        status=str(values.get("status") or ""),
        lock_path=str(values.get("lock_path") or ""),
        pid=int(values.get("pid") or 0),
        message=str(values.get("message") or ""),
    )


def _trace_written_dispatch_report(manager: Any, report: DispatchReport) -> DispatchReport:
    from ...debug_trace_reports import trace_dispatch_report

    return trace_dispatch_report(manager, report)


def _trace_written_dispatch_watch_report(
    manager: Any,
    report: DispatchWatchReport,
) -> DispatchWatchReport:
    from ...debug_trace_reports import trace_dispatch_watch_report

    return trace_dispatch_watch_report(manager, report)
