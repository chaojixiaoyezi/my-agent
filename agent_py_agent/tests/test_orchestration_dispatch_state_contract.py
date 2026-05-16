"""Focused tests for top-level dispatch state contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool


# LLM: _dispatch_agent_with_state builds a minimal agent whose load() exposes mixed run statuses.
# 函数用途: 构造 dispatch_subagents 测试用 agent，避免状态合同测试塞进大型 child-refs suite。
def _dispatch_agent_with_state(tasks: dict[str, SimpleNamespace]):
    report = MagicMock()
    report.dry_run = False
    report.summary = {}
    report.records = []
    mock_agent = MagicMock()
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.config.runner_timeout_seconds = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.load.side_effect = lambda run_id: tasks[str(run_id)]
    mock_agent.subagents.list_runs.return_value = list(tasks.values())
    return mock_agent


# LLM: Top-level dispatch should expose the current run-state contract without reading bulky records.
# 函数用途: root 显式 dispatch 多个 run 后，要能直接看到哪些可跑、哪些在跑、哪些阻塞、哪些已验收。
def test_dispatch_execute_payload_includes_current_turn_run_state():
    tasks = {
        "planning": SimpleNamespace(id="planning", status="PLANNING", verification_status="UNVERIFIED"),
        "running": SimpleNamespace(id="running", status="RUNNING", verification_status="UNVERIFIED"),
        "blocked": SimpleNamespace(id="blocked", status="BLOCKED", verification_status="UNVERIFIED"),
        "done": SimpleNamespace(id="done", status="DONE", verification_status="VERIFIED"),
    }
    payload = json.loads(DispatchSubagentsTool(_dispatch_agent_with_state(tasks)).execute({
        "apply": True,
        "execute_runners": True,
        "run_ids": ["planning", "running", "blocked", "done"],
    }).output)

    state = payload["current_turn_run_state"]
    assert state["by_status"] == {"PLANNING": 1, "RUNNING": 1, "BLOCKED": 1, "DONE": 1}
    assert state["dispatchable_run_ids"] == ["planning"]
    assert state["running_run_ids"] == ["running"]
    assert state["blocked_run_ids"] == ["blocked"]
    assert state["verified_run_ids"] == ["done"]
    assert state["next_action"] == "inspect_or_rescue_blocked_run_ids"
    assert state["suggested_tool_call"]["run_ids"] == ["blocked"]
