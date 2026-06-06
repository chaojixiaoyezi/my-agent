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


def test_inspect_collaboration_rejects_run_status_surface_without_requests(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": "run-123"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "WRONG_STATUS_SURFACE"
    assert payload["error"] == "wrong_status_surface"
    assert payload["request_count"] == 0
    assert payload["suggested_tool_call"]["tool"] == "inspect_agent_tree"
    assert payload["suggested_tool_call"]["run_id"] == "run-123"


def test_inspect_collaboration_without_case_or_request_is_not_status_surface(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent._current_run_params = SimpleNamespace(task_id="run-current")

    result = agent.tools.tools["inspect_collaboration"].execute({"limit": 10})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "WRONG_STATUS_SURFACE"
    assert payload["request_count"] == 0
    assert payload["suggested_tool_call"]["tool"] == "inspect_agent_tree"
