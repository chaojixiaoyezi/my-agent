
"""Dispatch log appending helper."""

from __future__ import annotations

from agent_py_agent.agent.io import append_jsonl


class DispatchLogAppender:
    """Write global dispatch audit log."""

    @staticmethod
    def append(record, workspace) -> None:
        """Write global dispatch audit log entry."""
        from dataclasses import asdict

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