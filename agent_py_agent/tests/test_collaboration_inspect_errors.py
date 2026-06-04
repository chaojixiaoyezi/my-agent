from __future__ import annotations

import json

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
