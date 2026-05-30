"""Tests for dispatch_subagents takeover defaults in runner context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _mock_dispatch_agent(current_run_id: str = "subagent-parent"):
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = current_run_id
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = []
    return mock_agent


def test_runner_context_dispatch_defaults_takeover_to_current_parent():
    """runner 里未传 take_over_by 时，接管动作默认由当前父级 run 执行。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    mock_agent = _mock_dispatch_agent("subagent-parent")

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

    assert result.ok is True
    params = mock_agent.dispatch_subagents.call_args.kwargs["params"]
    assert params.take_over_by == "subagent-parent"


def test_runner_context_dispatch_keeps_explicit_takeover_owner():
    """模型显式指定接管者时优先使用显式值，方便父级把任务转给新 leader。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    mock_agent = _mock_dispatch_agent("subagent-parent")

    result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False, "take_over_by": "subagent-new-leader"})

    assert result.ok is True
    params = mock_agent.dispatch_subagents.call_args.kwargs["params"]
    assert params.take_over_by == "subagent-new-leader"

