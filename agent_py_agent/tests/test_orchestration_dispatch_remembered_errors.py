from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


def test_dispatch_guidance_reports_remembered_run_load_errors() -> None:
    """已记住的子代理状态读取失败时，不能误报没有未完成子代理。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []

    mock_agent = MagicMock()
    mock_agent._orchestration_run_ids_seen = {"broken-run"}
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.load.side_effect = ValueError("remembered state broken")

    payload = json.loads(DispatchSubagentsTool(mock_agent).execute({"dry_run": False}).output)

    assert payload["completion_risk"] is True
    assert payload["completion_status"]["completion_risk"] is True
    assert payload["unfinished_load_errors"][0]["run_id"] == "broken-run"
    assert payload["unfinished_load_errors"][0]["category"] == "data_parse"
    assert payload["next_action"] == "refresh_agent_tree_or_rebuild_state_index"
