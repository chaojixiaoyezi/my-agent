from __future__ import annotations

"""LLM: tests for subagent_mixin.

给人看的解释：
测试子代理生命周期管理：spawn、run_subagent、run_parent_planner、recovery snapshot 等。
"""

from pathlib import Path
from unittest.mock import MagicMock, NonCallableMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.agent_core.subagent_mixin import (
    SimpleAgentSubagentMixin,
    _config_workflow_dispatch_mode,
)


class TestSubagentMixinSpawn:
    """测试 spawn_subagents 方法。"""

    def test_spawn_subagents_disabled(self) -> None:
        """测试子代理被禁用时抛出异常。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = False

        with pytest.raises(RuntimeError, match="配置已禁用 subagent"):
            mixin.spawn_subagents("测试目标")

    def test_spawn_subagents_with_count(self) -> None:
        """测试显式指定数量时的 spawn。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 3
        mixin.config.subagent_workflow_mode = "off"
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task, mock_task]

        result = mixin.spawn_subagents("测试目标", count=2)
        assert len(result) == 2
        mixin.subagents.split.assert_called_once()

    def test_spawn_subagents_count_exceeds_max(self) -> None:
        """测试数量超过 max_subagents 时的限制。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 2
        mixin.config.subagent_workflow_mode = "off"
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task, mock_task]

        result = mixin.spawn_subagents("测试目标", count=5)

        # 验证 split 被调用了
        assert mixin.subagents.split.called
        # 验证返回了两个 task（被限制为 max_subagents=2）
        assert len(result) == 2


class TestSubagentMixinRun:
    """测试 run_subagent 方法。"""

    @pytest.fixture
    def mock_mixin(self) -> SimpleAgentSubagentMixin:
        """创建测试用 mixin。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 3
        mixin.config.subagent_workflow_mode = "off"
        mixin.root = MagicMock()
        mixin.session_id = "test-session"
        return mixin

    def test_run_subagent_dry_run(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试 dry-run 模式。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id=""
        )

        mock_mixin.subagents.write_execution_context.return_value = MagicMock(
            allowed_tools=["tool1"],
            write_boundary={},
            goal="test goal",
        )

        # 创建一个真实的 MagicMock 用于 record_runner_result 返回值
        mock_result = MagicMock()
        mock_result.run_id = "test-run"
        mock_mixin.subagents.record_runner_result.return_value = mock_result

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin._build_subagent_runner_prompt"
        ) as mock_prompt_builder:
            mock_prompt_builder.return_value = "built prompt"

            result = mock_mixin.run_subagent(
                run_id="test-run",
                dry_run=True,
                instruction="test instruction",
            )

        assert result.run_id == "test-run"
        mock_mixin.subagents.record_runner_result.assert_called_once()
        call_kwargs = mock_mixin.subagents.record_runner_result.call_args[1]
        assert call_kwargs["dry_run"] is True

    def test_run_subagent_with_attempt_id(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试带 attempt_id 的 run。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id="attempt-123"
        )
        mock_mixin.subagents.write_execution_context.return_value = MagicMock(
            allowed_tools=[],
            write_boundary={},
            goal="goal",
        )
        mock_result = MagicMock()
        mock_result.run_id = "run-id"
        mock_mixin.subagents.record_runner_result.return_value = mock_result

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin._build_subagent_runner_prompt"
        ) as mock_prompt_builder:
            mock_prompt_builder.return_value = "built prompt"

            result = mock_mixin.run_subagent(
                run_id="run-id",
                dry_run=True,
                attempt_id="attempt-123",
            )

        assert result.run_id == "run-id"

    def test_run_subagent_channel_broken(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试通道健康检查失败。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id=""
        )
        mock_mixin.subagents.write_execution_context.return_value = MagicMock(
            allowed_tools=[],
            write_boundary={},
            goal="goal",
        )
        mock_mixin.subagents.probe_channel.return_value = MagicMock(channel_status="BROKEN")
        mock_result = MagicMock()
        mock_result.run_id = "test"
        mock_mixin.subagents.record_runner_result.return_value = mock_result

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin._build_subagent_runner_prompt"
        ) as mock_prompt_builder:
            mock_prompt_builder.return_value = "built prompt"

            result = mock_mixin.run_subagent(
                run_id="test",
                dry_run=False,
                probe=True,
            )

        mock_mixin.subagents.record_runner_result.assert_called_once()
        call_kwargs = mock_mixin.subagents.record_runner_result.call_args[1]
        assert call_kwargs["status"] == "CHANNEL_ERROR"
        assert call_kwargs["ok"] is False


class TestSubagentMixinParentPlanner:
    """测试 run_parent_planner 方法。"""

    @pytest.fixture
    def mock_mixin(self) -> SimpleAgentSubagentMixin:
        """创建测试用 mixin。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.agent_name = "test-agent"
        mixin.root = MagicMock()
        mixin.session_id = "test-session"
        return mixin

    def test_run_parent_planner_no_work_needed(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试无工作需要时直接返回。"""
        mock_mixin.subagents = MagicMock()

        # 设置 mock 返回值
        mock_record = MagicMock()
        mock_record.decision = "HEARTBEAT_OK"
        mock_record.triggered = False
        mock_mixin.subagents.make_parent_planner_record.return_value = mock_record
        mock_mixin.subagents.build_parent_planner_report.return_value = MagicMock()
        mock_mixin.subagents.write_parent_planner_report.return_value = None

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin._build_parent_planner_state"
        ) as mock_state:
            mock_state.return_value = {
                "gate": {"needs_planner": 0},
                "tasks": [],
            }
            from agent_py_agent.agent.agent_core.subagent_mixin import RunParentPlannerParams

            result = mock_mixin.run_parent_planner(
                RunParentPlannerParams(
                    router=MagicMock(),
                    capability_config=None,
                    apply=False,
                    execute_runners=False,
                    max_runners=1,
                    limit=20,
                    reviewer="parent-dispatch",
                    note="",
                    runner_instruction="",
                )
            )

        assert result.decision == "HEARTBEAT_OK"
        assert result.triggered is False


class TestSubagentMixinRecoverySnapshot:
    """测试 recovery snapshot 相关方法。"""

    @pytest.fixture
    def mock_mixin(self) -> SimpleAgentSubagentMixin:
        """创建测试用 mixin。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.memory_hook_enabled = True
        mixin.root = MagicMock()
        mixin.session_id = "test-session"
        return mixin

    def test_write_recovery_snapshot_disabled(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试 memory hook 禁用时不写 snapshot。"""
        mock_mixin.config.memory_hook_enabled = False

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot"
        ) as mock_write:
            mock_mixin._write_subagent_recovery_snapshot(
                run_id="test-run",
                user_prompt="test prompt",
                response_text="response",
                backend="test",
                status="ok",
                error_code="",
                tool_calls=[],
            )
            mock_write.assert_not_called()

    def test_write_recovery_snapshot_task_not_found(
        self, mock_mixin: SimpleAgentSubagentMixin
    ) -> None:
        """测试任务不存在时的处理。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.load.side_effect = FileNotFoundError()

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot"
        ) as mock_write:
            mock_write.return_value = MagicMock()
            mock_mixin._write_subagent_recovery_snapshot(
                run_id="test-run",
                user_prompt="prompt",
                response_text="response",
                backend="test",
                status="ok",
                error_code="",
                tool_calls=[],
            )
            mock_write.assert_called_once()

    def test_write_recovery_snapshot_success(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试成功写入 snapshot。"""
        mock_mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mock_task.status_file = "/status/file"
        mock_task.work_log_file = "/worklog/file"
        mock_task.runner_result_file = "/result/file"
        mock_task.runner_result_json = "/result/json"
        mock_task.output_json = "/output/json"
        mock_task.handoff_file = "/handoff/file"
        mock_mixin.subagents.load.return_value = mock_task

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot"
        ) as mock_write:
            mock_write.return_value = MagicMock()
            mock_mixin._write_subagent_recovery_snapshot(
                run_id="test-run",
                user_prompt="prompt",
                response_text="response",
                backend="test",
                status="ok",
                error_code="",
                tool_calls=[{"tool": "test_tool", "id": "1", "ok": True}],
            )
            mock_write.assert_called_once()
            call_kwargs = mock_write.call_args[1]
            assert call_kwargs["params"].source == "subagent_run"
            assert call_kwargs["params"].run_id == "test-run"


class TestSubagentMixinWorkflowDispatch:
    """测试 workflow dispatch 配置。"""

    def test_config_workflow_dispatch_mode_auto(self) -> None:
        """测试 auto 模式转换。"""
        result = _config_workflow_dispatch_mode("auto")
        assert result == "auto"

    def test_config_workflow_dispatch_mode_manual(self) -> None:
        """测试 manual 模式转换为 plan。"""
        result = _config_workflow_dispatch_mode("manual")
        assert result == "plan"

    def test_config_workflow_dispatch_mode_off(self) -> None:
        """测试 off 模式。"""
        result = _config_workflow_dispatch_mode("off")
        assert result == "off"

    def test_config_workflow_dispatch_mode_invalid(self) -> None:
        """测试无效值默认为 off。"""
        result = _config_workflow_dispatch_mode("invalid")
        assert result == "off"

    def test_config_workflow_dispatch_mode_whitespace(self) -> None:
        """测试带空白的值。"""
        result = _config_workflow_dispatch_mode("  auto  ")
        assert result == "auto"

    def test_config_workflow_dispatch_mode_case_insensitive(self) -> None:
        """测试大小写不敏感。"""
        result = _config_workflow_dispatch_mode("AUTO")
        assert result == "auto"

    def test_config_workflow_dispatch_mode_none(self) -> None:
        """测试 None 值。"""
        result = _config_workflow_dispatch_mode(None)
        assert result == "off"
