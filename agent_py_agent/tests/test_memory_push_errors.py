from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_push_relevant_memories_report_keeps_search_failure_visible() -> None:
    from agent_py_agent.agent.memory_push import push_relevant_memories_report

    agent = SimpleNamespace(memory=MagicMock())
    agent.memory.search.side_effect = RuntimeError("memory search crashed")

    memories, load_errors = push_relevant_memories_report(
        agent,
        "timeout",
        {"task_id": "task-1", "goal": "long task", "failure_type": "timeout"},
        limit=3,
    )

    assert memories == []
    assert load_errors
    assert load_errors[0]["context"] == "memory_push.search"
    assert "memory search crashed" in load_errors[0]["message"]


def test_push_relevant_memories_report_preserves_memory_index_errors() -> None:
    from agent_py_agent.agent.memory_push import push_relevant_memories_report

    record = SimpleNamespace(
        content="This is a useful timeout lesson with enough detail",
        kind="lesson_general",
        tags=["timeout"],
        created_at=1.0,
    )
    index_error = {"context": "memory_store.local_store.search", "message": "index failed"}
    memory = MagicMock()
    memory.search_report.return_value = ([record], [index_error])
    agent = SimpleNamespace(memory=memory)

    memories, load_errors = push_relevant_memories_report(
        agent,
        "timeout",
        {"task_id": "task-1", "goal": "long task", "failure_type": "timeout"},
        limit=3,
    )

    assert memories == ["[lesson_general] This is a useful timeout lesson with enough detail"]
    assert load_errors == [index_error]
