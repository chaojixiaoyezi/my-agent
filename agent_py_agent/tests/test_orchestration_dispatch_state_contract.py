"""Focused tests for top-level dispatch state contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.action_protocol import decode_action_envelope
from agent_py_agent.agent.agent_core.hierarchy_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.agent_core.orchestration_tools import (
    CreateSubagentsTool,
    DispatchSubagentsTool,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


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


def test_dispatch_execute_payload_includes_current_turn_run_state():
    tasks = {
        "planning": SimpleNamespace(id="planning", status="PLANNING", verification_status="UNVERIFIED"),
        "running": SimpleNamespace(id="running", status="RUNNING", verification_status="UNVERIFIED"),
        "blocked": SimpleNamespace(
            id="blocked",
            status="BLOCKED",
            verification_status="UNVERIFIED",
            failure_type="TOOL_UNAVAILABLE",
        ),
        "done": SimpleNamespace(id="done", status="DONE", verification_status="VERIFIED"),
    }
    payload = json.loads(DispatchSubagentsTool(_dispatch_agent_with_state(tasks)).execute({
        "dry_run": False,
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
    assert state["state_machine_contract"] == "state_machine.v1"
    assert state["recovery_recommendations"] == [{
        "run_id": "blocked",
        "status": "BLOCKED",
        "failure_type": "TOOL_UNAVAILABLE",
        "recommended_action": "request_capability",
        "allow_new_run": False,
        "reason": "blocked_tool_unavailable",
        "recovery_hint": "工具不可用；查看 ToolManifest，换可执行工具或申请能力。",
    }]


def test_dispatch_state_reports_load_errors_instead_of_empty_state():
    agent = _dispatch_agent_with_state({})
    agent.subagents.load.side_effect = ValueError("state json broken")

    payload = json.loads(DispatchSubagentsTool(agent).execute({
        "dry_run": False,
        "run_ids": ["broken-run"],
    }).output)

    state = payload["current_turn_run_state"]
    assert state["missing_run_ids"] == ["broken-run"]
    assert state["task_load_errors"][0]["run_id"] == "broken-run"
    assert state["task_load_errors"][0]["category"] == "data_parse"
    assert "不要把它当成子代理没产物" in state["task_load_errors"][0]["model_message"]
    assert state["next_action"] == "refresh_agent_tree_or_rebuild_state_index"


def test_create_payload_includes_current_turn_run_state(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "写一个高端现代家具品牌首页 index.html",
        "role": "worker",
    }).output)

    state = payload["current_turn_run_state"]
    assert payload["auto_start"]["status"] == "started"
    assert payload["auto_start"]["dispatch_mode"] == "background"
    assert payload["dispatch_run_ids"] == []
    assert state["dispatchable_run_ids"] == []
    assert (
        state["running_run_ids"] == payload["created_run_ids"]
        or state["verified_run_ids"] == payload["created_run_ids"]
    )
    assert state["next_action"] in {"wait_for_subagent_completion_event", "summarize_or_report_verified_runs"}
    envelope = decode_action_envelope(payload["typed_envelope"])
    assert envelope.current_turn_run_state["dispatchable_run_ids"] == []
    assert envelope.dispatch_run_ids == []


def test_dispatch_state_running_subagent_suggests_wait_not_polling():
    tasks = {
        "running": SimpleNamespace(id="running", status="RUNNING", verification_status="UNVERIFIED"),
    }
    payload = json.loads(DispatchSubagentsTool(_dispatch_agent_with_state(tasks)).execute({
        "dry_run": False,
        "run_ids": ["running"],
    }).output)

    state = payload["current_turn_run_state"]
    assert state["next_action"] == "wait_for_subagent_completion_event"
    assert state["suggested_tool_call"]["tool"] == "wait"
    assert state["suggested_tool_call"]["seconds"] >= 10


def test_create_payload_includes_stable_operation_contract(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    params = {"goal": "写一个高端现代家具品牌首页 index.html", "role": "worker"}
    first = json.loads(CreateSubagentsTool(agent).execute(params).output)
    second = json.loads(CreateSubagentsTool(agent).execute(params).output)

    assert first["operation_contract"]["operation"] == "create_subagents"
    assert first["operation_contract"]["idempotency_key"] == second["operation_contract"]["idempotency_key"]
    assert first["operation_contract"]["operation_id"] == second["operation_contract"]["operation_id"]
    assert second["reused_run_ids"] == []
    assert second["created_run_ids"]


def test_schedule_child_payload_includes_current_turn_run_state(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id

    payload = json.loads(ScheduleChildSubagentsTool(agent).execute({
        "dry_run": False,
        "children": [{"goal": "写条目卡片组件", "role": "worker"}],
    }).output)

    state = payload["current_turn_run_state"]
    assert state["dispatchable_run_ids"] == []
    assert (
        state["running_run_ids"] == payload["created_run_ids"]
        or state["verified_run_ids"] == payload["created_run_ids"]
    )
    assert state["next_action"] in {"wait_for_subagent_completion_event", "summarize_or_report_verified_runs"}
    envelope = decode_action_envelope(payload["typed_envelope"])
    assert envelope.current_turn_run_state["dispatchable_run_ids"] == []
    assert envelope.dispatch_run_ids == payload["dispatch_run_ids"]
