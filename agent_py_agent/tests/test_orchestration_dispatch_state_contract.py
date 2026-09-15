"""Focused tests for top-level dispatch state contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.action_protocol import decode_action_envelope
from agent_py_agent.agent.agent_core.orchestration.dispatch.state_contract import (
    dispatch_state_contract_payload,
)
from agent_py_agent.agent.agent_core.orchestration.run_scope import (
    remember_orchestration_run_ids,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent_with_state(tasks: dict[str, SimpleNamespace]):
    mock_agent = MagicMock()
    mock_agent.subagents.load.side_effect = lambda run_id: tasks[str(run_id)]
    remember_orchestration_run_ids(mock_agent, tasks)
    return mock_agent


def _state_for_tasks(tasks: dict[str, SimpleNamespace]) -> dict[str, object]:
    return dispatch_state_contract_payload(_agent_with_state(tasks))["current_turn_run_state"]


def test_state_contract_includes_current_turn_run_state():
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
    state = _state_for_tasks(tasks)
    assert state["by_status"] == {"PLANNING": 1, "RUNNING": 1, "BLOCKED": 1, "DONE": 1}
    assert state["dispatchable_run_ids"] == ["planning"]
    assert state["running_run_ids"] == ["running"]
    assert state["blocked_run_ids"] == ["blocked"]
    assert state["completed_run_ids"] == ["done"]
    assert "next_action" not in state
    assert "suggested_tool_call" not in state
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
    agent = _agent_with_state({})
    remember_orchestration_run_ids(agent, ["broken-run"])
    agent.subagents.load.side_effect = ValueError("state json broken")

    state = dispatch_state_contract_payload(agent)["current_turn_run_state"]
    assert state["missing_run_ids"] == ["broken-run"]
    assert state["task_load_errors"][0]["run_id"] == "broken-run"
    assert state["task_load_errors"][0]["category"] == "data_parse"
    assert "不要把它当成子代理没产物" in state["task_load_errors"][0]["model_message"]
    assert "next_action" not in state


def test_create_payload_includes_current_turn_run_state(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "写一个高端现代家具品牌首页 index.html",
        "role": "worker",
    }).output)

    state = payload["current_turn_run_state"]
    assert payload["auto_start"]["status"] == "started"
    assert payload["auto_start"]["dispatch_mode"] == "background"
    assert payload["pending_start_run_ids"] == []
    assert state["dispatchable_run_ids"] == []
    assert (
        sorted(state["starting_run_ids"]) == sorted(payload["created_run_ids"])
        or sorted(state["running_run_ids"]) == sorted(payload["created_run_ids"])
        or sorted(state["completed_run_ids"]) == sorted(payload["created_run_ids"])
    )
    assert "next_action" not in state
    assert payload["next_action"]["action"] == "continue_independent_work"
    assert payload["schedule_lifecycle"]["start_accepted_run_ids"] == payload["created_run_ids"]
    assert payload["schedule_lifecycle"]["counts"]["running"] in {0, 1}
    envelope = decode_action_envelope(payload["typed_envelope"])
    assert envelope.current_turn_run_state["dispatchable_run_ids"] == []
    assert envelope.pending_start_run_ids == []


@pytest.mark.parametrize("status", ["PLANNING", "PENDING", "RUNNING", "BLOCKED", "DONE"])
def test_dispatch_state_does_not_infer_parent_work_from_child_status(status):
    tasks = {
        "child": SimpleNamespace(id="child", status=status, verification_status="UNVERIFIED"),
    }
    state = _state_for_tasks(tasks)
    assert "next_action" not in state
    assert "suggested_tool_call" not in state
    assert state["by_status"] == {status: 1}


def test_fast_done_child_is_visible_alongside_running_sibling_without_wait_advice():
    state = _state_for_tasks({
        "slow": SimpleNamespace(id="slow", status="RUNNING", goal="让父级一直等待"),
        "fast": SimpleNamespace(id="fast", status="DONE", goal="先做好供父级接入"),
    })
    assert state["running_run_ids"] == ["slow"]
    assert state["completed_run_ids"] == ["fast"]
    assert "next_action" not in state
    assert "goal" not in json.dumps(state, ensure_ascii=False)


def test_dispatch_state_completed_alias_does_not_suggest_closeout():
    tasks = {
        "alias": SimpleNamespace(id="alias", status="COMPLETED", verification_status="VERIFIED"),
    }
    state = _state_for_tasks(tasks)
    assert state["by_status"] == {"BLOCKED": 1}
    assert state["blocked_run_ids"] == ["alias"]
    assert state["completed_run_ids"] == []
    assert state["unfinished_run_ids"] == ["alias"]
    assert "next_action" not in state
    assert "suggested_tool_call" not in state
    assert state["recovery_recommendations"][0]["failure_type"] == "STATE_STATUS_INVALID"
    assert state["recovery_recommendations"][0]["recommended_action"] == "manual_review"


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


def test_nested_create_payload_includes_current_turn_run_state(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    agent._current_subagent_run_id = parent.id

    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "写条目卡片组件",
        "role": "worker",
    }).output)

    state = payload["current_turn_run_state"]
    assert state["dispatchable_run_ids"] == []
    assert (
        state["starting_run_ids"] == payload["created_run_ids"]
        or state["running_run_ids"] == payload["created_run_ids"]
        or state["completed_run_ids"] == payload["created_run_ids"]
    )
    assert "next_action" not in state
    envelope = decode_action_envelope(payload["typed_envelope"])
    assert envelope.current_turn_run_state["dispatchable_run_ids"] == []
    assert envelope.pending_start_run_ids == payload["pending_start_run_ids"]
