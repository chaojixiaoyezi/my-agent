from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_inspect_collaboration_pending_request_error_is_structured(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    def broken_pending_requests_for_agent_report(**_kwargs):
        raise OSError("pending request ledger unavailable")

    monkeypatch.setattr(
        agent.collaboration_store,
        "pending_requests_for_agent_report",
        broken_pending_requests_for_agent_report,
    )

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": "agent-b"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "pending_requests_read_failed"
    assert payload["load_error"]["context"] == "inspect_collaboration.pending_requests"
    assert payload["load_error"]["category"] == "io"


def test_inspect_collaboration_empty_requests_are_not_tool_failure(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    result = agent.tools.tools["inspect_collaboration"].execute({"run_id": "run-123"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["ok"] is True
    assert payload["request_count"] == 0
    assert payload["empty_reason"] == "no_pending_collaboration_requests"
    assert payload["suggested_tool_call"]["tool"] == "inspect_agent_tree"
    assert payload["suggested_tool_call"]["run_id"] == "run-123"


def test_inspect_collaboration_without_case_or_request_returns_empty_projection(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent._current_run_params = SimpleNamespace(task_id="run-current")

    result = agent.tools.tools["inspect_collaboration"].execute({"limit": 10})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["ok"] is True
    assert payload["request_count"] == 0
    assert payload["empty_reason"] == "no_pending_collaboration_requests"


def test_inspect_collaboration_known_subagent_id_suggests_agent_tree(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    monkeypatch.setattr(agent.subagents, "list_runs", lambda: [SimpleNamespace(id="subagent-123")])

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": "subagent-123"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "wrong_status_surface"
    assert payload["agent_id"] == "subagent-123"
    assert payload["identity_load_error"]["context"] == "collaboration.identity.subagents.load"
    assert payload["suggested_tool_call"] == {"tool": "inspect_agent_tree"}
