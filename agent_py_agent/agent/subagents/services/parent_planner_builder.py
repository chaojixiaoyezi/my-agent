"""Parent planner record, report and log helpers."""

from __future__ import annotations

import time


class ParentPlannerBuilder:
    """Build parent planner records and reports."""

    @staticmethod
    def make_record(manager, *, dry_run, triggered, ok, decision, message, gate_summary, backend, tool_rounds, parse_error, summary, actions, blockers, risks, notes, runner_instruction, suggested_max_runners, prompt_path, response_path, evidence_paths) -> ParentPlannerRecord:
        """Create a parent planner audit record."""
        from agent_py_agent.agent.subagents.reports import ParentPlannerRecord

        return ParentPlannerRecord(
            id=manager._new_id("planner"),
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