from __future__ import annotations

import json

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.messages.runtime_tool import MessageRuntimeTool


def test_message_runtime_tool_sends_and_reads_inbox(tmp_path):
    tool = MessageRuntimeTool(tmp_path)

    sent = tool.execute(
        {
            "action": "send",
            "sender": "session:sess-a",
            "target": "session:sess-b",
            "content": "hello",
            "task_id": "task-1",
        }
    )
    read = tool.execute({"action": "read_inbox", "target": "session:sess-b"})

    assert sent.ok is True
    assert read.ok is True
    messages = json.loads(read.output)["messages"]
    assert len(messages) == 1
    assert messages[0]["content"] == "hello"
    assert messages[0]["task_id"] == "task-1"


def test_message_runtime_tool_acks_message(tmp_path):
    tool = MessageRuntimeTool(tmp_path)
    sent = tool.execute(
        {
            "action": "send",
            "sender": "system:runtime",
            "target": "user:user-1",
            "content": "stored",
        }
    )
    message_id = json.loads(sent.output)["message"]["message_id"]

    ack = tool.execute({"action": "ack", "target": "user:user-1", "message_id": message_id})
    read = tool.execute({"action": "read_inbox", "target": "user:user-1"})

    assert ack.ok is True
    assert json.loads(read.output)["messages"] == []


def test_message_runtime_tool_rejects_unknown_target_shape(tmp_path):
    tool = MessageRuntimeTool(tmp_path)

    result = tool.execute(
        {
            "action": "send",
            "sender": "bad-target",
            "target": "session:sess-b",
            "content": "hello",
        }
    )

    assert result.ok is False
    assert result.error_code


def test_message_runtime_tool_is_registered_on_main_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    names = {spec.name for spec in agent.tools.specs()}

    assert "message_runtime" in names
    assert agent.runtime_messages_root == tmp_path / "data" / "runtime" / "messages"
