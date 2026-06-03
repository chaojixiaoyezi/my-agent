from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_archive import RawMemoryEvent, append_raw_event
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)


def _bulk_raw_event(index: int) -> RawMemoryEvent:
    return RawMemoryEvent(
        event_id=f"raw-bulk-{index:03d}",
        session_id="session-bulk",
        request_id="request-bulk",
        run_id=f"run-bulk-{index:03d}",
        task_id="task-bulk",
        speaker="user",
        target="assistant",
        action="message",
        status="ok",
        content_preview=f"bulk compact record {index}",
        source="run",
        archive_level=2,
        created_at=f"2026-05-06T08:{index // 60:02d}:{index % 60:02d}+00:00",
    )


def test_memory_compact_default_covers_all_matching_records(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    for index in range(60):
        append_raw_event(root, _bulk_raw_event(index))

    plan = build_memory_compact_plan(
        root,
        MemoryCompactPlanOptions(session_id="session-bulk", request_id="request-bulk"),
    )

    assert plan["scope"]["limit"] == 0
    assert plan["archive"]["record_count"] == 60
