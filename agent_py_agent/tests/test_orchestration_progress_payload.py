from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_runner_context_invalid_workflow_mode_stays_off() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "subagent-root"
    mock_agent.config.subagent_workflow_mode = "auto"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False, "workflow_mode": "parallel"})

    assert result.ok is True
    call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
    assert call_kwargs["params"].workflow_mode == "off"


def test_runner_context_dispatch_reports_direct_child_progress() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"runner": 1}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
        SimpleNamespace(id="child-a", parent_id="parent-run", status="DONE"),
        SimpleNamespace(id="child-b", parent_id="parent-run", status="PLANNING"),
    ]

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    payload = json.loads(result.output)
    assert payload["direct_children"]["by_status"]["PLANNING"] == 1
    assert payload["direct_children"]["planning_run_ids"] == ["child-b"]
    assert "继续调用 dispatch_subagents" in payload["direct_children"]["continue_hint"]


def test_runner_context_dispatch_suggests_wait_for_running_direct_child() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"runner": 1}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
        SimpleNamespace(id="child-running", parent_id="parent-run", status="RUNNING"),
    ]

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    direct = json.loads(result.output)["direct_children"]
    assert direct["next_action"] == "wait_for_running_direct_children"
    assert direct["suggested_tool_call"]["tool"] == "wait"
    assert "不要因为等待而重复" in direct["wait_hint"]


def test_runner_context_dispatch_keeps_completed_alias_under_status_review() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"runner": 1}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
        SimpleNamespace(id="child-alias", parent_id="parent-run", status="COMPLETED"),
    ]

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    direct = json.loads(result.output)["direct_children"]
    assert direct["ready_for_closeout"] is False
    assert direct["needs_status_review"] is True
    assert direct["unverified_run_ids"] == ["child-alias"]
    assert direct["next_action"] == "inspect_unverified_direct_children"
    assert direct["suggested_tool_call"] == {"tool": "inspect_agent_tree"}


def test_runner_context_dispatch_suggests_recovery_child_for_blocked_direct_child() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
        SimpleNamespace(id="child-blocked", parent_id="parent-run", status="BLOCKED"),
    ]

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    payload = json.loads(result.output)
    direct_children = payload["direct_children"]
    assert direct_children["needs_recovery"] is True
    suggestion = direct_children["suggested_recovery_child_tool_call"]
    assert suggestion["tool"] == "schedule_child_subagents"
    assert suggestion["children"][0]["role"] == "worker"
    assert "默认用 worker" in suggestion["role_selection_hint"]
    assert "child-blocked" in suggestion["children"][0]["goal"]


def test_runner_context_dispatch_reports_recovery_child_load_error() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
        SimpleNamespace(id="child-broken", parent_id="parent-run", status="BLOCKED"),
    ]
    mock_agent.subagents.load.side_effect = RuntimeError("child state unreadable")

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    direct_children = json.loads(result.output)["direct_children"]
    assert direct_children["needs_recovery"] is True
    assert direct_children["recovery_load_errors"][0]["run_id"] == "child-broken"
    assert direct_children["recovery_load_errors"][0]["context"] == "direct_children.recovery_task.load"


def test_runner_context_dispatch_reports_quality_advice_parent_load_error() -> None:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "parent-run"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = [
        SimpleNamespace(id="child-done", parent_id="parent-run", status="DONE"),
    ]
    mock_agent.subagents.load.side_effect = RuntimeError("parent state unreadable")

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    direct_children = json.loads(result.output)["direct_children"]
    assert direct_children["ready_for_closeout"] is True
    assert direct_children["quality_advice_load_error"]["context"] == "direct_children.quality_advice.parent_load"
    assert "parent state unreadable" in direct_children["quality_advice_load_error"]["message"]


def test_runner_context_dispatch_includes_packet_first_recovery_strategy(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派 child", plan=["schedule"], role="coordinator")
    child_id = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[HierarchyChildSpec(goal="继续 checkout", role="worker", agent_name="小傻妞-checkout")],
        )
    ).created_run_ids[0]
    child = manager.load(child_id)
    child.status = "BLOCKED"
    child.current_step = "从 packet 继续 checkout QA"
    child.latest_summary = "checkout 页面已写，等待恢复。"
    manager.save(child)

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = parent.id
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents = manager

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    payload = json.loads(result.output)
    direct = payload["direct_children"]
    strategy = direct["recovery_strategies"][0]
    suggested = direct["suggested_tool_call"]
    assert strategy["recommended_action"] == "retry"
    assert strategy["recovery_mode"] == "rerun_from_continue_packet"
    assert strategy["packet_status"] == "ready"
    assert strategy["uses_continue_packet"] is True
    assert strategy["task_envelope"]["address"]["lineage"] == [parent.id, child_id]
    assert strategy["task_envelope"]["acceptance"]["checks"]
    assert "latest_continue_packet.json" in suggested["runner_instruction"]
    assert suggested["run_ids"] == [child_id]


def test_runner_context_dispatch_batches_multiple_recovery_strategies_without_shared_instruction(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派多个 child", plan=["schedule"], role="coordinator")
    created = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="写登录", role="worker", agent_name="小傻妞-auth"),
                HierarchyChildSpec(goal="写流程状态", role="worker", agent_name="小傻妞-cart"),
            ],
        )
    ).created_run_ids
    for run_id in created:
        child = manager.load(run_id)
        child.status = "BLOCKED"
        manager.save(child)

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = parent.id
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents = manager

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    direct = json.loads(result.output)["direct_children"]
    assert len(direct["recovery_strategies"]) == 2
    assert direct["recovery_action_counts"]["retry"] == 2
    assert direct["recovery_mode_counts"]["rerun_from_continue_packet"] == 2
    assert direct["recovery_batches"][0]["recovery_mode"] == "rerun_from_continue_packet"
    assert direct["recovery_batches"][0]["recommended_action"] == "retry"
    assert set(direct["recovery_batches"][0]["run_ids"]) == set(created)
    assert set(direct["suggested_tool_call"]["run_ids"]) == set(created)
    assert "runner_instruction" not in direct["suggested_tool_call"]


def test_runner_context_dispatch_splits_mixed_recovery_batches(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派多个 child", plan=["schedule"], role="coordinator")
    blocked_id, timeout_id = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="继续登录", role="worker", agent_name="小傻妞-auth"),
                HierarchyChildSpec(goal="继续流程状态", role="worker", agent_name="小傻妞-cart"),
            ],
        )
    ).created_run_ids
    _set_child_state(manager, blocked_id, status="BLOCKED")
    _set_child_state(manager, timeout_id, status="TIMEOUT", failure_type="runner_timeout")

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = parent.id
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents = manager

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    batches = json.loads(result.output)["direct_children"]["recovery_batches"]
    by_mode = {item["recovery_mode"]: item for item in batches}
    rerun = by_mode["rerun_from_continue_packet"]
    takeover = by_mode["takeover_from_continue_packet"]
    assert rerun["run_ids"] == [blocked_id]
    assert rerun["recommended_action"] == "retry"
    assert rerun["suggested_tool_call"]["dry_run"] is False
    assert "runner_instruction" in rerun["suggested_tool_call"]
    assert takeover["run_ids"] == [timeout_id]
    assert takeover["recommended_action"] == "takeover"
    assert takeover["execution_mode"] == "takeover_apply"
    assert takeover["suggested_tool_call"]["dry_run"] is True
    assert takeover["suggested_tool_call"]["max_runners"] == 0


def _set_child_state(manager: SubAgentManager, run_id: str, *, status: str, failure_type: str = "") -> None:
    child = manager.load(run_id)
    child.status = status
    if failure_type:
        child.failure_type = failure_type
    manager.save(child)
