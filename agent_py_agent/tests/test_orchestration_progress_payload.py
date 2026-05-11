from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool


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
