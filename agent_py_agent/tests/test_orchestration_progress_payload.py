from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_runner_context_invalid_workflow_mode_stays_off protects runner-local workflow scoping.
# 函数用途: 模型误传 parallel 时，runner dispatch 也不能回退成全局 auto。
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

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "workflow_mode": "parallel"})

    assert result.ok is True
    call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
    assert call_kwargs["params"].workflow_mode == "off"


# LLM: test_runner_context_dispatch_reports_direct_child_progress keeps parent runners from misreading PLANNING.
# 函数用途: runner 内 dispatch 结果要提示剩余 PLANNING child，避免误判失败。
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
        SimpleNamespace(id="child-a", parent_id="parent-run", status="AWAITING_ACCEPTANCE"),
        SimpleNamespace(id="child-b", parent_id="parent-run", status="PLANNING"),
    ]

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

    payload = json.loads(result.output)
    assert payload["direct_children"]["by_status"]["PLANNING"] == 1
    assert payload["direct_children"]["planning_run_ids"] == ["child-b"]
    assert "继续调用 dispatch_subagents" in payload["direct_children"]["continue_hint"]


# LLM: test_runner_context_dispatch_suggests_recovery_child_for_blocked_direct_child protects role-flexible recovery.
# 函数用途: 直接 child 阻塞时，父 runner 要拿到可执行恢复建议，但不能被强制成 coordinator。
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

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

    payload = json.loads(result.output)
    direct_children = payload["direct_children"]
    assert direct_children["needs_recovery"] is True
    suggestion = direct_children["suggested_recovery_child_tool_call"]
    assert suggestion["tool"] == "schedule_child_subagents"
    assert suggestion["children"][0]["role"] == "worker"
    assert "默认用 worker" in suggestion["role_selection_hint"]
    assert "child-blocked" in suggestion["children"][0]["goal"]


# LLM: test_runner_context_dispatch_includes_packet_first_recovery_strategy protects root-only recovery.
# 函数用途: 父 runner 看到阻塞 child 时，dispatch 响应要先给 latest_continue_packet 续跑策略。
def test_runner_context_dispatch_includes_packet_first_recovery_strategy(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派 child", plan=["schedule"], role="coordinator")
    child_id = manager.schedule_child_runs(
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

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

    payload = json.loads(result.output)
    direct = payload["direct_children"]
    strategy = direct["recovery_strategies"][0]
    suggested = direct["suggested_tool_call"]
    assert strategy["recommended_action"] == "rerun_original_from_continue_packet"
    assert strategy["packet_status"] == "ready"
    assert strategy["uses_continue_packet"] is True
    assert strategy["task_envelope"]["address"]["lineage"] == [parent.id, child_id]
    assert strategy["task_envelope"]["acceptance"]["checks"]
    assert "latest_continue_packet.json" in suggested["runner_instruction"]
    assert suggested["run_ids"] == [child_id]


# LLM: test_runner_context_dispatch_batches_multiple_recovery_strategies_without_shared_instruction covers fan-out failure.
# 函数用途: 多个 child 同时失败时，父级要拿到批量策略，但不能把单个 runner_instruction 串给所有 child。
def test_runner_context_dispatch_batches_multiple_recovery_strategies_without_shared_instruction(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派多个 child", plan=["schedule"], role="coordinator")
    created = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="写登录", role="worker", agent_name="小傻妞-auth"),
                HierarchyChildSpec(goal="写购物车", role="worker", agent_name="小傻妞-cart"),
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

    result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

    direct = json.loads(result.output)["direct_children"]
    assert len(direct["recovery_strategies"]) == 2
    assert direct["recovery_action_counts"]["rerun_original_from_continue_packet"] == 2
    assert set(direct["suggested_tool_call"]["run_ids"]) == set(created)
    assert "runner_instruction" not in direct["suggested_tool_call"]
