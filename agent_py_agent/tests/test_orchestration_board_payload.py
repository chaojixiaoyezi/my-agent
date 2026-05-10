"""LLM: focused tests for model-friendly subagent board payload handling.

模块用途: 验证 subagent_board 的 ALL/ANY 过滤别名和可操作 run id 输出，避免真实 runner 查板时丢孩子。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# LLM: _board_item builds minimal board row fixtures for board-tool payload tests.
# 函数用途: 构造带状态、目标和 task_dir 的假看板条目，避免测试重复 mock 字段。
def _board_item(run_id: str, status: str):
    item = MagicMock()
    item.id = run_id
    item.goal = "测试"
    item.status = status
    item.verification_status = "UNVERIFIED"
    item.channel_status = "UNKNOWN"
    item.risk_flags = []
    item.evidence_count = 0
    item.open_request_count = 0
    item.open_gap_count = 0
    item.task_dir = f"/tmp/{run_id}"
    return item


# LLM: test_status_all_keeps_board_items protects the real runner's natural status=ALL query.
# 函数用途: status=ALL 表示查看全部，不应该把看板过滤空，同时要保留 actionable_run_ids。
def test_status_all_keeps_board_items():
    from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_board = MagicMock()
    mock_board.summary = {"total": 1}
    mock_board.items = [_board_item("run_1", "PLANNING")]
    mock_agent.subagents.write_board.return_value = mock_board

    result = SubagentBoardTool(mock_agent).execute({"status": "ALL", "limit": 10})

    assert result.ok is True
    assert '"returned": 1' in result.output
    assert '"planning": [' in result.output
