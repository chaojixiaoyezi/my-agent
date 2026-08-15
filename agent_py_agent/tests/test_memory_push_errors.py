from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.memory_push import push_relevant_memories_report
from agent_py_agent.tests._memory_push_v2_harness import formal_memory_agent, promote_lesson


def test_missing_owner_route_authority_has_visible_error() -> None:
    memories, errors = push_relevant_memories_report(
        SimpleNamespace(),
        "timeout",
        {"task_id": "task-1", "goal": "long task", "failure_type": "timeout"},
    )

    assert memories == []
    assert errors
    assert errors[0]["context"] == "memory_push.formal_recall"
    assert "authority" in errors[0]["message"]


def test_route_findings_are_preserved_as_structured_diagnostics(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    agent.home_paths.owner_memory_routing_index_md.write_text(
        "## bad\nauthority_path: ../../escape.md\n",
        encoding="utf-8",
    )

    memories, errors = push_relevant_memories_report(
        agent,
        "failure",
        {"goal": "bad"},
    )

    assert memories == []
    assert errors
    assert all(error["code"] == "MEMORY_ROUTING_FINDING" for error in errors)


def test_corrupt_formal_hot_is_not_partially_recalled(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    agent.memory_hot.path.write_text("# unmarked legacy HOT\n- unsafe\n", encoding="utf-8")

    memories, errors = push_relevant_memories_report(
        agent,
        "failure",
        {"goal": "失败验证"},
    )

    assert [record.kind for record in memories] == ["lesson"]
    assert "unsafe" not in memories[0].content
    assert errors == []


def test_invalid_limit_fails_soft() -> None:
    memories, errors = push_relevant_memories_report(
        SimpleNamespace(),
        "failure",
        {"goal": "x"},
        limit="invalid",  # type: ignore[arg-type]
    )

    assert memories == []
    assert errors
    assert errors[0]["context"] == "memory_push.formal_recall"
