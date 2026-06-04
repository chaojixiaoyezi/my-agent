"""planner.py 单元测试。

测试父代理 planner 状态快照构建、提示词生成和指令合并功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCombineRunnerInstruction:
    """测试 _combine_runner_instruction() 函数。"""

    def test_combine_both_non_empty(self):
        """CLI 指令和 planner 指令都存在时合并。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            combine_runner_instruction as _combine_runner_instruction,
        )

        result = _combine_runner_instruction("CLI 指令", "Planner 补充")

        assert "CLI 指令" in result
        assert "父代理 planner 补充指令" in result
        assert "Planner 补充" in result

    def test_combine_only_base(self):
        """只有 CLI 指令时返回 CLI 指令。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            combine_runner_instruction as _combine_runner_instruction,
        )

        result = _combine_runner_instruction("只有 CLI", "")

        assert result == "只有 CLI"

    def test_combine_only_planner(self):
        """只有 planner 指令时返回 planner 指令。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            combine_runner_instruction as _combine_runner_instruction,
        )

        result = _combine_runner_instruction("", "只有 Planner")

        assert result == "只有 Planner"

    def test_combine_both_empty(self):
        """都为空时返回空字符串。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            combine_runner_instruction as _combine_runner_instruction,
        )

        result = _combine_runner_instruction("", "")

        assert result == ""

    def test_combine_strips_whitespace(self):
        """验证空格被正确去除。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            combine_runner_instruction as _combine_runner_instruction,
        )

        result = _combine_runner_instruction("  CLI  ", "  Planner  ")

        assert not result.startswith(" ")
        assert not result.endswith(" ")


class TestTaskStateForPlanner:
    """测试 _task_state_for_planner() 函数。"""

    def test_task_state_basic_fields(self):
        """验证基本字段被正确提取。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            task_state_for_planner as _task_state_for_planner,
        )

        mock_task = MagicMock()
        mock_task.id = "run_123"
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.goal = "测试任务"
        mock_task.owner = "parent"
        mock_task.final_owner = ""
        mock_task.updated_at = 1234567890.0
        mock_task.heartbeat_at = 1234567800.0
        mock_task.evidence = [MagicMock(), MagicMock()]
        mock_task.capability_requests = [MagicMock(), MagicMock(), MagicMock()]
        mock_task.capability_requests[0].status = "OPEN"
        mock_task.capability_requests[1].status = "GRANTED"
        mock_task.capability_requests[2].status = "OPEN"
        mock_task.capability_gaps = [MagicMock()]
        mock_task.capability_gaps[0].status = "FILLED"

        state = _task_state_for_planner(mock_task)

        assert state["run_id"] == "run_123"
        assert state["status"] == "RUNNING"
        assert state["verification_status"] == "PENDING"
        assert state["channel_status"] == "OK"
        assert state["goal"] == "测试任务"
        assert state["owner"] == "parent"
        assert state["evidence_count"] == 2
        assert state["open_request_count"] == 2
        assert state["open_gap_count"] == 0

    def test_task_state_empty_evidence(self):
        """空 evidence 列表时返回 0。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            task_state_for_planner as _task_state_for_planner,
        )

        mock_task = MagicMock()
        mock_task.id = "run_empty"
        mock_task.status = "PLANNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.goal = ""
        mock_task.owner = ""
        mock_task.final_owner = ""
        mock_task.updated_at = 0.0
        mock_task.heartbeat_at = 0.0
        mock_task.evidence = []
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        state = _task_state_for_planner(mock_task)

        assert state["evidence_count"] == 0
        assert state["open_request_count"] == 0
        assert state["open_gap_count"] == 0


class TestBuildParentPlannerPrompt:
    """测试 _build_parent_planner_prompt() 函数。"""

    def test_prompt_contains_mode(self):
        """验证生成的 prompt 包含 mode 信息。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_prompt as _build_parent_planner_prompt,
        )

        state = {
            "gate": {"needs_planner": 1},
            "board_summary": {"total": 0},
        }

        prompt = _build_parent_planner_prompt(
            state,
            apply=True,
            start_runners=True,
            max_runners=2,
            runner_instruction="测试指令",
        )

        assert "mode: apply" in prompt
        assert "start_runners: True" in prompt
        assert "cli_max_runners: 2" in prompt
        assert "测试指令" in prompt

    def test_prompt_dry_run_mode(self):
        """验证 dry-run 模式的 prompt 生成。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_prompt as _build_parent_planner_prompt,
        )

        state = {"gate": {"needs_planner": 0}}

        prompt = _build_parent_planner_prompt(
            state,
            apply=False,
            start_runners=False,
            max_runners=1,
            runner_instruction="",
        )

        assert "mode: dry-run" in prompt

    def test_prompt_contains_state_snapshot(self):
        """验证 prompt 包含状态快照。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_prompt as _build_parent_planner_prompt,
        )

        state = {
            "gate": {"needs_planner": 1},
            "active_tasks": [{"run_id": "task_1", "status": "RUNNING"}],
        }

        prompt = _build_parent_planner_prompt(
            state,
            apply=True,
            start_runners=False,
            max_runners=1,
            runner_instruction="",
        )

        assert "task_1" in prompt
        assert "State Snapshot" in prompt

    def test_prompt_contains_result_block(self):
        """验证 prompt 包含结果块标记。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_prompt as _build_parent_planner_prompt,
        )

        state = {"gate": {"needs_planner": 1}}

        prompt = _build_parent_planner_prompt(
            state,
            apply=True,
            start_runners=False,
            max_runners=1,
            runner_instruction="",
        )

        assert "[PARENT_PLANNER_RESULT]" in prompt
        assert "[/PARENT_PLANNER_RESULT]" in prompt
        assert "decision" in prompt


class TestBuildParentPlannerState:
    """测试 _build_parent_planner_state() 函数。"""

    def test_state_contains_gate_summary(self):
        """验证状态包含 gate_summary。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_state as _build_parent_planner_state,
        )
        from agent_py_agent.agent.capability.config import CapabilityConfig

        mock_agent = MagicMock()
        mock_agent.subagents.list_runs.return_value = []
        mock_agent.subagents.build_board.return_value = MagicMock(summary={})
        mock_agent.subagents.due_check.return_value = MagicMock(summary={"total": 0})
        mock_agent.subagents.plan_actions.return_value = MagicMock(summary={"total": 0})
        mock_agent.subagents.review_acceptances.return_value = MagicMock(records=[])

        state = _build_parent_planner_state(
            mock_agent,
            CapabilityConfig(),
            max_runners=1,
            limit=10,
            reviewer="test",
            note="test note",
        )

        assert "gate" in state
        assert "needs_planner" in state["gate"]
        assert "total_tasks" in state["gate"]

    def test_state_gate_needs_planner_when_active_tasks(self):
        """有活跃任务时 gate.needs_planner 为 1。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_state as _build_parent_planner_state,
        )
        from agent_py_agent.agent.capability.config import CapabilityConfig

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []
        mock_task.evidence = []

        mock_agent = MagicMock()
        mock_agent.subagents.list_runs.return_value = [mock_task]
        mock_agent.subagents.build_board.return_value = MagicMock(summary={})
        mock_agent.subagents.due_check.return_value = MagicMock(summary={"total": 0}, issues=[])
        mock_agent.subagents.plan_actions.return_value = MagicMock(summary={"total": 0}, actions=[])
        mock_agent.subagents.review_acceptances.return_value = MagicMock(records=[])

        state = _build_parent_planner_state(
            mock_agent,
            CapabilityConfig(),
            max_runners=1,
            limit=10,
            reviewer="test",
            note="test note",
        )

        assert state["gate"]["active_tasks"] == 1
        assert state["gate"]["needs_planner"] == 1

    def test_state_calculates_open_capability_requests(self):
        """验证 open_capability_requests 数量计算。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_state as _build_parent_planner_state,
        )
        from agent_py_agent.agent.capability.config import CapabilityConfig

        mock_request = MagicMock()
        mock_request.status = "OPEN"
        mock_request.id = "req_1"
        mock_request.needed_capability = "test_cap"
        mock_request.problem = "问题"
        mock_request.expected_output = "输出"

        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = [mock_request]
        mock_task.capability_gaps = []
        mock_task.evidence = []

        mock_agent = MagicMock()
        mock_agent.subagents.list_runs.return_value = [mock_task]
        mock_agent.subagents.build_board.return_value = MagicMock(summary={})
        mock_agent.subagents.due_check.return_value = MagicMock(summary={"total": 0}, issues=[])
        mock_agent.subagents.plan_actions.return_value = MagicMock(summary={"total": 0}, actions=[])
        mock_agent.subagents.review_acceptances.return_value = MagicMock(records=[])

        state = _build_parent_planner_state(
            mock_agent,
            CapabilityConfig(),
            max_runners=1,
            limit=10,
            reviewer="test",
            note="test note",
        )

        assert state["gate"]["open_capability_requests"] == 1
        assert len(state["open_capability_requests"]) == 1

    def test_state_handles_empty_task_list(self):
        """空任务列表时不崩溃。"""
        from agent_py_agent.agent.agent_core.planner_service import (
            build_parent_planner_state as _build_parent_planner_state,
        )
        from agent_py_agent.agent.capability.config import CapabilityConfig

        mock_agent = MagicMock()
        mock_agent.subagents.list_runs.return_value = []
        mock_agent.subagents.build_board.return_value = MagicMock(summary={})
        mock_agent.subagents.due_check.return_value = MagicMock(summary={"total": 0}, issues=[])
        mock_agent.subagents.plan_actions.return_value = MagicMock(summary={"total": 0}, actions=[])
        mock_agent.subagents.review_acceptances.return_value = MagicMock(records=[])

        state = _build_parent_planner_state(
            mock_agent,
            CapabilityConfig(),
            max_runners=1,
            limit=10,
            reviewer="test",
            note="test note",
        )

        assert state["gate"]["total_tasks"] == 0
        assert state["gate"]["active_tasks"] == 0
        assert state["gate"]["needs_planner"] == 0


class TestRunParentPlannerErrors:
    """测试父代理 planner 调用异常能留给父代理查看。"""

    def test_parent_planner_runtime_error_is_recorded(self, tmp_path, monkeypatch):
        from agent_py_agent.agent.agent_core._subagent_planner_mixin import RunParentPlannerParams
        from agent_py_agent.agent.agent_core.orchestration.dispatch.params import (
            DispatchExecutionPlan,
        )
        from agent_py_agent.agent.capability import CapabilityRouter
        from agent_py_agent.agent.capability.config import CapabilityConfig
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

        monkeypatch.setattr(
            "agent_py_agent.agent.agent_core._subagent_planner_mixin._build_parent_planner_state",
            lambda *args, **kwargs: {"gate": {"needs_planner": 1, "active_tasks": 1}, "tasks": []},
        )

        def fail_run(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("provider returned 503")

        monkeypatch.setattr(agent, "run", fail_run)

        record = agent.run_parent_planner(
            RunParentPlannerParams(
                router=CapabilityRouter(),
                capability_config=CapabilityConfig(),
                execution_plan=DispatchExecutionPlan(preview_only=False, mutate_state=True),
                max_runners=1,
                limit=10,
                reviewer="test",
                note="",
                runner_instruction="",
            )
        )

        assert record.ok is False
        assert record.decision == "PLANNER_ERROR"
        assert record.runtime_error["context"] == "parent_planner.run"
        assert "provider returned 503" in str(record.runtime_error)


class TestParentPlannerReadTools:
    """测试 PARENT_PLANNER_READ_TOOLS 常量。"""

    def test_parent_planner_read_tools_defined(self):
        """验证只读工具列表已定义。"""
        from agent_py_agent.agent.agent_core.planner_service import PARENT_PLANNER_READ_TOOLS

        assert isinstance(PARENT_PLANNER_READ_TOOLS, list)
        assert "list_files" in PARENT_PLANNER_READ_TOOLS
        assert "read_file" in PARENT_PLANNER_READ_TOOLS
        assert "search_text" in PARENT_PLANNER_READ_TOOLS
