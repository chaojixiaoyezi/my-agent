
"""Dispatch watch log appending helper."""

from __future__ import annotations

from agent_py_agent.agent.io import append_jsonl


class DispatchWatchLogAppender:
    """Write global watch audit log."""

    @staticmethod
    def append(record, workspace, manager) -> None:
        """Write global watch audit log entry."""
        from dataclasses import asdict

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
        manager._index_dispatch_watch_record(record)