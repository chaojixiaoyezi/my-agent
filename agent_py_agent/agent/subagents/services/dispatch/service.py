
from __future__ import annotations

"""dispatch record and reporting service.

这里承接调度记录生成、汇总和写出逻辑。
SubAgentManager 通过当前服务组合调用这里。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_py_agent.agent.io import append_jsonl

from ...reports import (
    DispatchRecord,
    DispatchReport,
    DispatchWatchRecord,
    DispatchWatchReport,
)
from ..indexing.records import IndexReportParams
from .params import (
    DispatchRecordParams,
    DispatchWatchHeartbeatParams,
    DispatchWatchRecordParams,
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
        return _make_dispatch_record(self.manager, params)

    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        summary = _dispatch_summary(records)
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
            for record in report.records:
                _append_dispatch_log(record, self.manager.workspace)
        for record in report.records:
            self.manager.indexing.index_dispatch_record(record)
        self.manager.indexing.index_report(
            IndexReportParams(
                "subagent_dispatch_report", "latest",
                "Subagent dispatch report", report,
                "subagent_dispatch_report_written",
            ),
        )
        return _trace_written_dispatch_report(self.manager, report)

    def _append_dispatch_log(self, record: DispatchRecord) -> None:
        _append_dispatch_log(record, self.manager.workspace)

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
        params = params or _dispatch_watch_record_params(
            locals(),
        )
        return _make_dispatch_watch_record(self.manager, params)

    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        summary = _dispatch_watch_summary(records)
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
        self.manager.indexing.index_report(
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
        _append_dispatch_watch_log(record, self.manager.workspace, self.manager)


def _make_dispatch_record(manager: Any, params: DispatchRecordParams) -> DispatchRecord:
    return DispatchRecord(
        id=manager._new_id("dispatch"),
        step=params.step,
        action=params.action,
        run_id=params.run_id,
        dry_run=params.dry_run,
        applied=params.applied,
        ok=params.ok,
        message=params.message,
        before_status=params.before_status,
        after_status=params.after_status,
        before_verification_status=params.before_verification_status,
        after_verification_status=params.after_verification_status,
        evidence_paths=params.evidence_paths or [],
        # 字段用途: 让父级看到 runner 真实创建的下级数量、id 和角色，后续按 refs 继续处理。
        runner_summary=params.runner_summary,
        runner_created_child_count=params.runner_created_child_count,
        runner_created_child_ids=params.runner_created_child_ids or [],
        runner_created_roles=params.runner_created_roles or [],
        runner_child_status_counts=params.runner_child_status_counts or {},
        runner_unfinished_child_ids=params.runner_unfinished_child_ids or [],
        runner_child_load_errors=params.runner_child_load_errors or [],
        collaboration_candidate_load_errors=params.collaboration_candidate_load_errors or [],
        runner_partial_success=params.runner_partial_success,
        runner_instruction=params.runner_instruction,
        suggested_max_runners=params.suggested_max_runners,
        created_at=time.time(),
    )


def _dispatch_summary(records: list[DispatchRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.step] = summary.get(record.step, 0) + 1
        summary[record.action] = summary.get(record.action, 0) + 1
        summary["ok" if record.ok else "failed"] = summary.get(
            "ok" if record.ok else "failed", 0,
        ) + 1
        summary["applied" if record.applied else "dry_run"] = summary.get(
            "applied" if record.applied else "dry_run", 0,
        ) + 1
        summary["runner_created_children"] = summary.get(
            "runner_created_children", 0,
        ) + int(record.runner_created_child_count or 0)
    return summary


def _append_dispatch_log(record: DispatchRecord, workspace: Path) -> None:
    jsonl = workspace / "subagent_dispatch_log.jsonl"
    append_jsonl(jsonl, asdict(record))

    markdown = workspace / "DISPATCH_LOG.md"
    if not markdown.exists():
        markdown.write_text("# DISPATCH LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        handle.write(
            f"- [{status}] {record.id} step={record.step} action={record.action} "
            f"run={run} applied={record.applied} message={record.message}\n"
        )


def _make_dispatch_watch_record(manager: Any, params: DispatchWatchRecordParams) -> DispatchWatchRecord:
    return DispatchWatchRecord(
        id=manager._new_id("watch"),
        cycle=params.cycle,
        dry_run=params.dry_run,
        ok=params.ok,
        message=params.message,
        dispatch_record_count=params.dispatch_record_count,
        dispatch_summary=params.dispatch_summary or {},
        started_at=params.started_at,
        ended_at=params.ended_at,
        evidence_paths=params.evidence_paths or [],
    )


def _dispatch_watch_summary(records: list[DispatchWatchRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary["ok" if record.ok else "failed"] = summary.get(
            "ok" if record.ok else "failed", 0,
        ) + 1
        summary["dry_run" if record.dry_run else "applied"] = summary.get(
            "dry_run" if record.dry_run else "applied", 0,
        ) + 1
        summary["dispatch_records"] = summary.get("dispatch_records", 0) + record.dispatch_record_count
    return summary


def _append_dispatch_watch_log(record: DispatchWatchRecord, workspace: Path, manager: Any) -> None:
    jsonl = workspace / "subagent_dispatch_watch_log.jsonl"
    append_jsonl(jsonl, asdict(record))

    markdown = workspace / "DISPATCH_WATCH_LOG.md"
    if not markdown.exists():
        markdown.write_text("# DISPATCH WATCH LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        status = "OK" if record.ok else "FAIL"
        handle.write(
            f"- [{status}] {record.id} cycle={record.cycle} "
            f"records={record.dispatch_record_count} message={record.message}\n"
        )
    manager.indexing.index_dispatch_watch_record(record)


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
