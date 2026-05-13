from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_archive import RawMemoryEvent, append_raw_event
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)


# LLM: _bulk_raw_event proves default compact coverage does not silently drop older matching records.
# 函数用途: 构造同一 compact scope 下的多条 raw 记录，用来验证默认 compact 不再截断 50 条。
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


# LLM: Default compact should cover all matching records; users should not guess a magic limit.
# 函数用途: 验证没有显式 --limit 时，compact 默认不截断最近 50 条，而是覆盖当前 scope 全部匹配记录。
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
