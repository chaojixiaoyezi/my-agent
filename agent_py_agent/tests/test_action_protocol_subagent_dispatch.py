"""Tests for typed dispatch_subagents action envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.action_protocol import SubagentDispatchEnvelope, decode_action_envelope
from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool


# LLM: test_dispatch_subagents_output_contains_typed_envelope protects refs-first dispatch handoff.
# 函数用途: 确认 dispatch_subagents 返回机器可读 envelope，父级不用解析自然语言 message。
def test_dispatch_subagents_output_contains_typed_envelope():
    report = SimpleNamespace(
        dry_run=False,
        summary={"total": 1, "runner": 1},
        records=[SimpleNamespace(
            step="runner",
            action="execute",
            run_id="child-1",
            ok=True,
            dry_run=False,
            applied=True,
            message="done",
            before_status="PLANNING",
            after_status="AWAITING_ACCEPTANCE",
        )],
    )
    agent = MagicMock()
    agent.config.subagent_workflow_mode = "off"
    agent.config.runner_timeout_seconds = "off"
    agent.tools.specs.return_value = []
    agent.dispatch_subagents.return_value = report
    agent.subagents.workspace = Path("/tmp/subagents")
    agent.subagents.list_runs.return_value = []

    result = DispatchSubagentsTool(agent).execute({"apply": True})
    payload = json.loads(result.output)
    envelope = decode_action_envelope(payload["typed_envelope"])

    assert isinstance(envelope, SubagentDispatchEnvelope)
    assert envelope.kind == "subagent_dispatch"
    assert envelope.operation_id == "subagent_dispatch:dispatch_subagents:/tmp/subagents/subagent_dispatch_report.json"
    assert envelope.dispatch_json == "/tmp/subagents/subagent_dispatch_report.json"
    assert envelope.actionable_run_ids == ["child-1"]
    assert envelope.record_count == 1
