from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_push import (
    format_memories_for_injection,
    push_failure_memories,
    push_planning_memories,
    push_relevant_memories_report,
)
from agent_py_agent.tests._memory_push_v2_harness import (
    formal_memory_agent,
    promote_hot,
    promote_lesson,
)


def test_formal_lesson_file_routes_into_one_envelope(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(
        agent,
        content="超时后先检查真实进程状态，再决定是否延长超时。",
        subject_key="failure.timeout.process-state",
    )

    records = push_failure_memories(agent, "task-1", "进程超时", "timeout")
    rendered = format_memories_for_injection(records)

    assert [record.entry_id for record in records] == [lesson.lesson_id]
    assert "先检查真实进程状态" in rendered
    assert rendered.count("<memory-context>") == 1


def test_formal_hot_rule_and_lesson_do_not_duplicate_detailed_body(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(agent)
    hot = promote_hot(agent, lesson, rule="失败后先验证证据。")

    records = push_failure_memories(agent, "task-1", "失败验证", "failure")

    assert [record.entry_id for record in records] == [hot.hot_id, lesson.lesson_id]
    assert records[0].content == "失败后先验证证据。"
    assert records[1].content.startswith("规划或失败后先验证")


def test_routing_index_is_navigation_not_prompt_authority(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    index = agent.home_paths.owner_memory_routing_index_md
    original = index.read_text(encoding="utf-8")
    assert "authority_path:" in original

    records = push_planning_memories(agent, "task-1", "规划失败验证")
    rendered = format_memories_for_injection(records)

    assert "authority_path:" not in rendered
    assert "trigger_keywords:" not in rendered


def test_recall_does_not_change_candidate_or_lesson_files(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    candidate_before = agent.memory_candidates.path.read_bytes()
    lesson_paths = list(agent.memory_lessons.lessons_dir.glob("*.md"))
    lesson_before = {path: path.read_bytes() for path in lesson_paths}

    for _ in range(3):
        assert push_failure_memories(agent, "task-1", "失败验证", "failure")

    assert agent.memory_candidates.path.read_bytes() == candidate_before
    assert {path: path.read_bytes() for path in lesson_paths} == lesson_before


def test_corrupt_formal_lesson_fails_closed(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(agent)
    lesson_path = agent.home_paths.owner_home_dir / lesson.path
    lesson_path.write_text("# missing formal marker\nunsafe legacy body\n", encoding="utf-8")

    memories, errors = push_relevant_memories_report(
        agent,
        "failure",
        {"goal": "失败验证"},
    )

    assert memories == []
    assert errors
    assert errors[0]["context"] == "memory_push.formal_recall"


def test_corrupt_routing_index_fails_closed_without_legacy_fallback(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    agent.home_paths.owner_memory_routing_index_md.write_text(
        "## broken\nauthority_path: ../../escape.md\n",
        encoding="utf-8",
    )
    agent.memory.add("system", "旧 long-term 教训", kind="lesson_general")

    memories, errors = push_relevant_memories_report(
        agent,
        "failure",
        {"goal": "失败验证"},
    )

    assert memories == []
    assert errors


def test_unrelated_formal_lesson_is_not_routed(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="发布前检查数据库迁移。",
        subject_key="database.release.migration",
    )

    records = push_failure_memories(agent, "task-1", "图像颜色校正", "render")

    assert records == []


def test_limit_zero_is_a_noop(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    memories, errors = push_relevant_memories_report(
        agent,
        "failure",
        {"goal": "失败验证"},
        limit=0,
    )

    assert memories == []
    assert errors == []


def test_internal_candidate_schema_is_not_rendered(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    rendered = format_memories_for_injection(
        push_failure_memories(agent, "task-1", "失败验证", "failure")
    )

    assert "candidate_id" not in rendered
    assert "promotion_target" not in rendered
    assert "review_note" not in rendered


def test_formal_envelope_payload_is_valid_json(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    rendered = format_memories_for_injection(
        push_failure_memories(agent, "task-1", "失败验证", "failure")
    )
    payload = rendered.splitlines()[2]

    decoded = json.loads(payload)

    assert decoded["schema"] == "my-agent.memory-context.v1"
    assert decoded["authority"] == "non_authoritative"
    assert decoded["records"][0]["kind"] == "lesson"


def test_formal_recall_does_not_call_long_term_search(tmp_path: Path, monkeypatch) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    monkeypatch.setattr(
        agent.memory,
        "search",
        lambda *_args, **_kwargs: pytest.fail("legacy long-term search must not run"),
    )

    assert push_failure_memories(agent, "task-1", "失败验证", "failure")
