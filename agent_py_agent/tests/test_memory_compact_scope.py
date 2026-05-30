from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_archive import RawMemoryEvent, append_raw_event
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.tests.memory_compact_support import (
    append_compact_token_usage,
    compact_raw_event,
)


# LLM: Compact restore refs point at shared JSONL files, so work-state extraction must re-filter records.
# 函数用途: 验证同一天其它 session/task 的 archive 记录不会把验收事实串进当前 compact work_state。
def test_memory_compact_work_state_filters_shared_archive_files_by_scope(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_scoped_and_leaking_archives(root)
    _write_scoped_and_leaking_fact_sources(root)

    result = apply_memory_compact(root, MemoryCompactApplyOptions(_scoped_plan_options()))

    assert result["work_state_snapshot"]["acceptance"]["items"] == ["scoped acceptance"]


# LLM: _scoped_plan_options keeps the test focused on the exact scope that should survive JSONL filtering.
# 函数用途: 构造当前 compact 的 session/request/run/task 过滤条件。
def _scoped_plan_options() -> MemoryCompactPlanOptions:
    return MemoryCompactPlanOptions(
        session_id="session-compact",
        request_id="request-compact",
        run_id="run-compact",
        task_id="run-compact",
    )


# LLM: _write_scoped_and_leaking_archives creates same-file archive rows for positive and negative cases.
# 函数用途: 写入一条当前 scope raw 事件和一条无关事件，模拟同日 shared JSONL。
def _write_scoped_and_leaking_archives(root: Path) -> None:
    append_raw_event(root, compact_raw_event())
    append_raw_event(root, _leaking_raw_event())
    append_compact_token_usage(root)


# LLM: _leaking_raw_event is intentionally close to the real event shape but outside the active scope.
# 函数用途: 生成不应被当前 compact work_state 读取的无关 archive 记录。
def _leaking_raw_event() -> RawMemoryEvent:
    return RawMemoryEvent(
        event_id="raw-leak",
        session_id="session-leak",
        request_id="request-leak",
        run_id="run-leak",
        task_id="run-leak",
        speaker="user",
        target="assistant",
        action="message",
        status="ok",
        content_preview="unrelated task should not leak",
        source="run",
        archive_level=2,
        created_at="2026-05-06T08:03:00+00:00",
    )


# LLM: _write_scoped_and_leaking_fact_sources proves task/run roots are also constrained by scope refs.
# 函数用途: 分别写入当前 run 和泄漏 run 的 ACCEPTANCE.md，断言只读取当前 run。
def _write_scoped_and_leaking_fact_sources(root: Path) -> None:
    good_dir = root / "subagents" / "run-compact"
    leak_dir = root / "subagents" / "run-leak"
    good_dir.mkdir(parents=True)
    leak_dir.mkdir(parents=True)
    (good_dir / "ACCEPTANCE.md").write_text("- scoped acceptance\n", encoding="utf-8")
    (leak_dir / "ACCEPTANCE.md").write_text("- leaked acceptance\n", encoding="utf-8")
