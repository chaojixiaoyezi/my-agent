"""Dispatch watch record and report helpers."""

from __future__ import annotations

import time


class DispatchWatchBuilder:
    """Build dispatch watch records and reports."""

    @staticmethod
    def make_record(manager, *, cycle, dry_run, ok, message, dispatch_record_count, dispatch_summary, started_at, ended_at, evidence_paths) -> DispatchWatchRecord:
        """Create a watch loop record."""
        from agent_py_agent.agent.subagents.reports import DispatchWatchRecord

        return DispatchWatchRecord(
            id=manager._new_id("watch"),
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

    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize watch loop records."""
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