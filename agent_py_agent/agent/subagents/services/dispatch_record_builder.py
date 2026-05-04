"""Dispatch record creation and building helpers."""

from __future__ import annotations

import time


class DispatchRecordBuilder:
    """Build dispatch audit records."""

    @staticmethod
    def make_record(manager, params) -> DispatchRecord:
        """Create a dispatch audit record from params bundle."""
        from agent_py_agent.agent.subagents.reports import DispatchRecord

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
            created_at=time.time(),
        )

    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize dispatch audit records."""
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
        return summary