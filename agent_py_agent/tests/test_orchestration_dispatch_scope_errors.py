from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.capability import CapabilityRouter


def test_nested_dispatch_excludes_known_parent_when_parent_load_fails() -> None:
    """父级 run_id 已知但父账本读坏时，也不能把父级重新纳入调度候选。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    current = SimpleNamespace(
        id="child-run",
        parent_id="parent-run",
        status="RUNNING",
        runner_active_attempt_id="a-child",
    )

    def load(run_id):
        if run_id == "child-run":
            return current
        if run_id == "parent-run":
            raise ValueError("parent state broken")
        raise KeyError(run_id)

    mock_agent = MagicMock()

    mock_agent.capability_router = CapabilityRouter()
    mock_agent._current_subagent_run_id = "child-run"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.load.side_effect = load

    result = DispatchSubagentsTool(mock_agent).execute({})

    assert result.ok is True
    params = mock_agent.dispatch_subagents.call_args.kwargs["params"]
    assert params.exclude_run_ids == ["child-run", "parent-run"]
