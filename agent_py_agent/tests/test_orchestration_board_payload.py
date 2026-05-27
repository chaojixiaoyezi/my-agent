"""LLM: focused tests for model-friendly subagent board payload handling.

模块用途: 验证 subagent_board 的 ALL/ANY 过滤别名和可操作 run id 输出，避免真实 runner 查板时丢孩子。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.subagents.kernel import SubagentKernelRun, SubagentKernelSnapshot


# LLM: _board_item builds minimal board row fixtures for board-tool payload tests.
# 函数用途: 构造带状态、目标和 task_dir 的假看板条目，避免测试重复 mock 字段。
def _board_item(run_id: str, status: str):
    item = MagicMock()
    item.id = run_id
    item.root_id = run_id
    item.parent_id = ""
    item.depth = 0
    item.agent_name = "小傻妞-worker"
    item.role = "worker"
    item.goal = "测试"
    item.status = status
    item.verification_status = "UNVERIFIED"
    item.channel_status = "UNKNOWN"
    item.risk_flags = []
    item.evidence_count = 0
    item.child_count = 0
    item.child_status_counts = {}
    item.open_request_count = 0
    item.open_gap_count = 0
    item.latest_summary = ""
    item.blocker_count = 0
    item.target_tokens = []
    item.artifact_refs = []
    item.evidence_refs = []
    item.task_dir = f"/tmp/{run_id}"
    item.output_json = f"/tmp/{run_id}/output.json"
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


# LLM: Board reads must tell the parent not to summarize incomplete child work as done.
# 函数用途: DONE/VERIFIED 看板条目要在顶层暴露阻塞 id 和建议 dispatch，不让模型看见文件就报完成。
def test_board_payload_marks_pending_closeout_as_not_complete():
    from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_board = MagicMock()
    mock_board.summary = {"total": 1, "DONE": 1, "VERIFIED": 1}
    item = _board_item("run_1", "DONE")
    item.verification_status = "VERIFIED"
    mock_board.items = [item]
    mock_agent.subagents.write_board.return_value = mock_board

    result = SubagentBoardTool(mock_agent).execute({"limit": 10})

    assert result.ok is True
    assert '"completion_status": {' in result.output
    assert '"run_1"' in result.output


# LLM: Board completion should match closeout when a verified repair covers stale failed work.
# 函数用途: 后续 VERIFIED 产物已经覆盖旧失败 run 时，看板不能继续提示父级重修同一个文件。
def test_board_payload_treats_verified_target_coverage_as_complete():
    from agent_py_agent.agent.agent_core.orchestration_board_payload import board_completion_status

    stale = _board_item("stale", "BLOCKED")
    stale.verification_status = "UNVERIFIED"
    stale.target_tokens = ["index1.html"]
    repair = _board_item("repair", "DONE")
    repair.verification_status = "VERIFIED"
    repair.target_tokens = ["index1.html"]

    status = board_completion_status([stale, repair])

    assert status["status"] == "complete_or_no_blockers"
    assert status["blocking_run_ids"] == []
    assert status["must_not_report_done"] is False


# LLM: Board payload must surface child deliverables so root agents do not guess task paths.
# 函数用途: 验证 subagent_board 顶层和条目都带 artifact/evidence refs，保护真实 E2E 的收集阶段。
def test_board_payload_includes_deliverable_refs_for_completed_children():
    from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_board = MagicMock()
    mock_board.summary = {"total": 1, "DONE": 1, "VERIFIED": 1}
    item = _board_item("run_1", "DONE")
    item.verification_status = "VERIFIED"
    item.artifact_refs = ["/tmp/site/final_report.md"]
    item.evidence_refs = ["/tmp/site/evidence.json"]
    mock_board.items = [item]
    mock_agent.subagents.write_board.return_value = mock_board

    result = SubagentBoardTool(mock_agent).execute({"limit": 10})

    assert result.ok is True
    assert '"deliverable_artifact_refs": [' in result.output
    assert '"/tmp/site/final_report.md"' in result.output
    assert '"deliverable_evidence_refs": [' in result.output
    assert '"/tmp/site/evidence.json"' in result.output
    assert '"artifact_refs": [' in result.output


# LLM: Active board scope prevents old workspace rows from steering the current root turn.
# 函数用途: 本轮已经创建 run_id 时，subagent_board 默认只返回本轮相关行，不混入旧失败测试。
def test_scoped_board_items_ignores_unseen_historical_rows():
    from agent_py_agent.agent.agent_core.orchestration_board_payload import scoped_board_items

    old = _board_item("old", "BLOCKED")
    old.verification_status = "FAILED"
    current = _board_item("current", "DONE")
    current.verification_status = "VERIFIED"
    agent = MagicMock()
    agent._orchestration_run_ids_seen = {"current"}

    items = scoped_board_items(agent, [old, current])

    assert [item.id for item in items] == ["current"]


def test_board_payload_includes_kernel_snapshot_when_available():
    from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

    class FakeSubagents:
        workspace = Path("/tmp/workspace")

        def write_board(self, options):
            board = MagicMock()
            board.summary = {"total": 1}
            board.items = [_board_item("run_1", "RUNNING")]
            return board

        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                root_id=query.root_id,
                scope=query.scope,
                running_run_ids=["run_1"],
                runs=[
                    SubagentKernelRun(
                        run_id="run_1",
                        root_id="run_1",
                        status="RUNNING",
                        role="worker",
                        tool_contract={"allowed_tools": ["read_file", "write_file"]},
                        workspace_refs={"agent_run_workspace": "/tmp/workspace/run_1"},
                        recovery_refs={"checkpoint": "/tmp/workspace/run_1/checkpoint.json"},
                    )
                ],
            )

    mock_agent = MagicMock()
    mock_agent.subagents = FakeSubagents()

    result = SubagentBoardTool(mock_agent).execute({"limit": 10})

    assert result.ok is True
    assert '"kernel_snapshot": {' in result.output
    assert '"running_run_ids": [' in result.output
    assert '"allowed_tools": [' in result.output
    assert '"agent_run_workspace": "/tmp/workspace/run_1"' in result.output
