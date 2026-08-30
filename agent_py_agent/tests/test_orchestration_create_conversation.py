"""Conversation refs attached to new subagents should expose bookkeeping errors."""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.create_policy import (
    add_current_conversation_attrs,
)
from agent_py_agent.agent.agent_core.orchestration.lifecycle import (
    _bind_tasks_to_conversation,
)


def test_conversation_ref_failures_are_structured_on_attrs() -> None:
    class BrokenConversationStore:
        def thread_for_task(self, task_id):
            del task_id
            raise ValueError("thread index broken")

        def get_or_create_thread(self, payload):
            del payload
            raise OSError("cannot create internal thread")

    attrs: dict[str, object] = {}
    agent = SimpleNamespace(
        conversation_store=BrokenConversationStore(),
        _current_run_params=SimpleNamespace(task_id="task-1", root_user_prompt="整理资料"),
    )

    add_current_conversation_attrs(attrs, agent)

    assert "conversation_thread_id" not in attrs
    lookup_error = attrs["conversation_thread_lookup_error"]
    materialize_error = attrs["conversation_thread_materialize_error"]
    assert lookup_error["context"] == "conversation.thread_for_task"
    assert lookup_error["category"] == "data_parse"
    assert materialize_error["context"] == "conversation.materialize_internal_thread"
    assert materialize_error["category"] == "io"


def test_conversation_bind_failure_is_reported_not_swallowed() -> None:
    class BrokenConversationStore:
        def bind_task(self, payload):
            del payload
            raise OSError("conversation index unavailable")

    agent = SimpleNamespace(conversation_store=BrokenConversationStore())
    task = SimpleNamespace(
        id="run-1",
        goal="整理项目报告",
        attributes={"conversation_thread_id": "thread-1"},
    )

    errors = _bind_tasks_to_conversation(agent, [task])

    assert errors[0]["run_id"] == "run-1"
    assert errors[0]["thread_id"] == "thread-1"
    assert errors[0]["context"] == "create_subagents.conversation.bind_task"
    assert errors[0]["category"] == "io"
    assert errors[0]["recoverable"] is True


def test_conversation_bind_reconciles_a_cancel_that_landed_before_projection() -> None:
    """停止可在慢批量创建途中先落 canonical；后绑的会话行不得复活为 active。"""

    class RecordingConversationStore:
        def __init__(self) -> None:
            self.bound: list[dict[str, object]] = []
            self.updated: list[dict[str, object]] = []

        def bind_task(self, payload):
            self.bound.append(dict(payload))
            return SimpleNamespace(**payload)

        def update_task_status(self, payload):
            self.updated.append(dict(payload))
            return SimpleNamespace(**payload)

    task = SimpleNamespace(
        id="run-cancelled-before-bind",
        goal="分析架构",
        status="PLANNING",
        task_dir="/tmp/task",
        attributes={"conversation_thread_id": "thread-1"},
    )
    canonical = SimpleNamespace(id=task.id, status="CANCELLED")
    store = RecordingConversationStore()
    agent = SimpleNamespace(
        conversation_store=store,
        subagents=SimpleNamespace(load=lambda run_id: canonical),
    )

    errors = _bind_tasks_to_conversation(agent, [task])

    assert errors == []
    assert store.bound[0]["status"] == "active"
    assert store.updated == [{"task_id": task.id, "status": "cancelled"}]
