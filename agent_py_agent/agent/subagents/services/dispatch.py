from __future__ import annotations

"""LLM: dispatch record and reporting service.

给人看的解释：
这里承接调度记录生成、汇总和写出逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

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
    ) -> "DispatchRecord":
        """Create a dispatch audit record."""
        from ..reports import DispatchRecord

        return DispatchRecord(
            id=self.manager._new_id("dispatch"),
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
            evidence_paths=evidence_paths or [],
            created_at=time.time(),
        )

    def build_dispatch_report(
        self,
        records: list["DispatchRecord"],
        *,
        dry_run: bool,
    ) -> "DispatchReport":
        """Summarize dispatch audit records."""
        from ..reports import DispatchReport

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
        return DispatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_report(
        self,
        report: "DispatchReport",
        *,
        append_log: bool = False,
    ) -> "DispatchReport":
        """Write dispatch report and optional audit log."""
        import json

        (self.manager.workspace / "subagent_dispatch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH.md").write_text(
            self.manager._render_dispatch_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            for record in report.records:
                self._append_dispatch_log(record)
        for record in report.records:
            self.manager._index_dispatch_record(record)
        self.manager._index_report(
            "subagent_dispatch_report", "latest",
            "Subagent dispatch report", report,
            event_type="subagent_dispatch_report_written",
        )
        return report

    def _append_dispatch_log(self, record: "DispatchRecord") -> None:
        """Write global dispatch audit log."""
        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "subagent_dispatch_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.manager.workspace / "DISPATCH_LOG.md"
        if not markdown.exists():
            markdown.write_text("# DISPATCH LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            run = record.run_id or "global"
            handle.write(
                f"- [{status}] {record.id} step={record.step} action={record.action} "
                f"run={run} applied={record.applied} message={record.message}\n"
            )
        self.manager._index_dispatch_record(record)

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
    ) -> "DispatchWatchRecord":
        """Create a watch loop record."""
        from ..reports import DispatchWatchRecord

        return DispatchWatchRecord(
            id=self.manager._new_id("watch"),
            cycle=cycle,
            dry_run=dry_run,
            ok=ok,
            message=message,
            dispatch_record_count=dispatch_record_count,
            dispatch_summary=dispatch_summary or {},
            started_at=started_at,
            ended_at=ended_at,
            evidence_paths=evidence_paths or [],
        )

    def build_dispatch_watch_report(
        self,
        records: list["DispatchWatchRecord"],
        *,
        dry_run: bool,
    ) -> "DispatchWatchReport":
        """Summarize watch loop records."""
        from ..reports import DispatchWatchReport

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied", 0,
            ) + 1
            summary["dispatch_records"] = summary.get("dispatch_records", 0) + record.dispatch_record_count
        return DispatchWatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_watch_report(self, report: "DispatchWatchReport") -> "DispatchWatchReport":
        """Write watch mode report."""
        import json

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
        import json

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
    ) -> "ParentPlannerRecord":
        """Create a parent planner audit record."""
        from ..reports import ParentPlannerRecord

        return ParentPlannerRecord(
            id=self.manager._new_id("planner"),
            dry_run=dry_run,
            triggered=triggered,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=gate_summary or {},
            backend=backend,
            tool_rounds=tool_rounds,
            parse_error=parse_error,
            summary=summary,
            actions=actions or [],
            blockers=blockers or [],
            risks=risks or [],
            notes=notes or [],
            runner_instruction=runner_instruction,
            suggested_max_runners=suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=evidence_paths or [],
            created_at=time.time(),
        )

    def build_parent_planner_report(
        self,
        records: list["ParentPlannerRecord"],
        *,
        dry_run: bool,
    ) -> "ParentPlannerReport":
        """Summarize parent planner records."""
        from ..reports import ParentPlannerReport

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["triggered" if record.triggered else "skipped"] = summary.get(
                "triggered" if record.triggered else "skipped", 0,
            ) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary[record.decision] = summary.get(record.decision, 0) + 1
        return ParentPlannerReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_parent_planner_report(
        self,
        report: "ParentPlannerReport",
        *,
        append_log: bool = False,
    ) -> "ParentPlannerReport":
        """Write parent planner report and optional audit log."""
        import json

        (self.manager.workspace / "parent_planner_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "PARENT_PLANNER.md").write_text(
            self.manager._render_parent_planner_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            for record in report.records:
                self.manager.append_parent_planner_log(record)
        for record in report.records:
            self.manager._index_parent_planner_record(record)
        self.manager._index_report(
            "parent_planner_report", "latest",
            "Parent planner report", report,
            event_type="parent_planner_report_written",
        )
        return report

    def append_dispatch_watch_log(self, record: "DispatchWatchRecord") -> None:
        """Write global watch audit log."""
        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "subagent_dispatch_watch_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.manager.workspace / "DISPATCH_WATCH_LOG.md"
        if not markdown.exists():
            markdown.write_text("# DISPATCH WATCH LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} cycle={record.cycle} "
                f"records={record.dispatch_record_count} message={record.message}\n"
            )
        self.manager._index_dispatch_watch_record(record)

    def append_parent_planner_log(self, record: "ParentPlannerRecord") -> None:
        """Write global parent planner audit log."""
        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "parent_planner_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.manager.workspace / "PARENT_PLANNER_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PARENT PLANNER LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} decision={record.decision} "
                f"triggered={record.triggered} message={record.message}\n"
            )
        self.manager._index_parent_planner_record(record)