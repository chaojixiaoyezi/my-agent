from __future__ import annotations

import json

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_raise_collaboration_reports_corrupt_explicit_thread(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    agent.conversation_store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "协作任务", "now": 2.0}
    )
    agent.conversation_store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "thread_id": thread.thread_id,
            "title": "账本读取失败 case",
            "summary": "读取 thread 失败时要把错误交给模型。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.reported_error_code == "THREAD_LOOKUP_FAILED"
    assert payload["error"] == "thread_lookup_failed"
    assert payload["load_error"]["context"] == "raise_collaboration.load_thread"
    assert payload["load_error"]["read_context"] == "conversation.thread.read"
    assert payload["load_error"]["thread_id"] == thread.thread_id
