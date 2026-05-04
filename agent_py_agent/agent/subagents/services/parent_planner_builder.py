"""Parent planner record, report and log helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ParentPlannerRecordParams:
    """Bundle of make_record parameters."""

    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] | None = None
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] | None = None
    blockers: list[str] | None = None
    risks: list[str] | None = None
    notes: list[str] | None = None
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] | None = None


class ParentPlannerBuilder:
    """Build parent planner records and reports."""

    @staticmethod
    def make_record(manager, *, params: ParentPlannerRecordParams) -> ParentPlannerRecord:
        """Create a parent planner audit record."""
        from agent_py_agent.agent.subagents.reports import ParentPlannerRecord

        return ParentPlannerRecord(
            id=manager._new_id("planner"),
            dry_run=params.dry_run,
            triggered=params.triggered,
            ok=params.ok,
            decision=params.decision,
            message=params.message,
            gate_summary=params.gate_summary or {},
            backend=params.backend,
            tool_rounds=params.tool_rounds,
            parse_error=params.parse_error,
            summary=params.summary,
            actions=params.actions or [],
            blockers=params.blockers or [],
            risks=params.risks or [],
            notes=params.notes or [],
            runner_instruction=params.runner_instruction,
            suggested_max_runners=params.suggested_max_runners,
            prompt_path=params.prompt_path,
            response_path=params.response_path,
            evidence_paths=params.evidence_paths or [],
            created_at=time.time(),
        )

    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize parent planner records."""
        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["triggered" if record.triggered else "skipped"] = summary.get(
                "triggered" if record.triggered else "skipped", 0,
            ) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary[record.decision] = summary.get(record.decision, 0) + 1
        return summary


class ParentPlannerLogAppender:
    """Write global parent planner audit log."""

    @staticmethod
    def append(record, workspace, manager) -> None:
        """Write global parent planner audit log entry."""
        from dataclasses import asdict

        from agent_py_agent.agent.file_io import append_jsonl

        jsonl = workspace / "parent_planner_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = workspace / "PARENT_PLANNER_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PARENT PLANNER LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} decision={record.decision} "
                f"triggered={record.triggered} message={record.message}\n"
            )
        manager._index_parent_planner_record(record)