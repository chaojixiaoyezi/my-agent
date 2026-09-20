from __future__ import annotations

"""LLM: tests for subagent_mixin.

给人看的解释：
测试子代理生命周期管理：spawn、run_subagent、run_parent_planner、recovery snapshot 等。
"""

from pathlib import Path
from unittest.mock import MagicMock, NonCallableMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.agent_core.subagent.params import SpawnSubagentsParams
from agent_py_agent.agent.agent_core.subagent.spawn_flow import configured_subagent_allowed_tools
from agent_py_agent.agent.agent_core.subagent_mixin import SimpleAgentSubagentMixin


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
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task, mock_task]

        result = mixin.spawn_subagents("测试目标", count=2)
        assert len(result) == 2
        mixin.subagents.split.assert_called_once()

    def test_spawn_subagents_without_count_uses_configured_default(self) -> None:
        """没有 count 时，工具调用本身就是明确创建意图，不再靠复杂度估算决定是否创建。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 10
        mixin.config.subagent_spawn_default_count = 4
        mixin.config.subagent_allowed_tools = []
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task, mock_task, mock_task, mock_task]

        result = mixin.spawn_subagents("测试目标")

        assert len(result) == 4
        mixin.subagents.split.assert_called_once()
        assert mixin.subagents.split.call_args.args[1] == 4

    def test_spawn_subagents_passes_configured_allowed_tools(self) -> None:
        """测试 spawn 时把配置里的默认工具白名单写入子任务。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 3
        mixin.config.subagent_allowed_tools = ["read_file", "write_file"]
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task]

        mixin.spawn_subagents("测试目标", count=1)

        call = mixin.subagents.split.call_args
        assert call.kwargs["allowed_tools"] == ["read_file", "write_file"]

    def test_spawn_subagents_empty_allowed_tools_uses_automatic_policy(self) -> None:
        """测试空 subagent_allowed_tools 不等于禁用工具，而是交给角色/任务推断。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 3
        mixin.config.subagent_allowed_tools = []
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.split.return_value = [mock_task]

        mixin.spawn_subagents("测试目标", count=1)

        call = mixin.subagents.split.call_args
        assert call.kwargs["allowed_tools"] is None

    def test_spawn_subagents_explicit_coordinator_creates_root_run(self) -> None:
        """显式 coordinator spawn 应创建真正 root/coordinator，而不是默认 worker split。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 1000
        mixin.config.subagent_allowed_tools = []
        mixin.config.subagent_role_template_dirs = []
        mixin.subagents = MagicMock()
        mock_task = MagicMock()
        mixin.subagents.create_run.return_value = mock_task

        result = mixin.spawn_subagents(
            params=SpawnSubagentsParams(
                goal="主节点只负责创建子代理",
                count=1,
                role="coordinator",
                agent_name="root-coordinator",
            )
        )

        assert result == [mock_task]
        mixin.subagents.split.assert_not_called()
        create_params = mixin.subagents.create_run.call_args.kwargs["params"]
        assert create_params.goal == "主节点只负责创建子代理"
        assert create_params.role == "coordinator"
        assert create_params.agent_name == "root-coordinator"
        assert create_params.parent_id == ""
        assert create_params.root_id == ""
        assert "create_subagents" in create_params.allowed_tools
        assert "schedule_child_subagents" not in create_params.allowed_tools
        assert "dispatch_subagents" not in create_params.allowed_tools
        assert "write_file" in create_params.allowed_tools

    def test_configured_subagent_allowed_tools_empty_means_automatic(self) -> None:
        """测试配置归一化：空字符串、空列表和缺省值都表示自动工具策略。"""
        config = MagicMock()
        config.subagent_allowed_tools = []
        assert configured_subagent_allowed_tools(config) is None

        config.subagent_allowed_tools = "  "
        assert configured_subagent_allowed_tools(config) is None

        config.subagent_allowed_tools = None
        assert configured_subagent_allowed_tools(config) is None

        config.subagent_allowed_tools = "read_file, write_file"
        assert configured_subagent_allowed_tools(config) == ["read_file", "write_file"]

    def test_spawn_subagents_count_exceeds_max(self) -> None:
        """测试数量超过 max_subagents 时的限制。"""
        mixin = SimpleAgentSubagentMixin()
        mixin.config = MagicMock()
        mixin.config.enable_subagents = True
        mixin.config.max_subagents = 2
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
        mixin.root = MagicMock()
        mixin.session_id = "test-session"
        return mixin

    def test_run_subagent_dry_run(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试 dry-run 模式。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.lifecycle.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id=""
        )

        mock_mixin.subagents.runner_context.write_execution_context.return_value = MagicMock(
            allowed_tools=["tool1"],
            write_boundary={},
            goal="test goal",
        )

        # 创建一个真实的 MagicMock 用于 record_runner_result 返回值
        mock_result = MagicMock()
        mock_result.run_id = "test-run"
        mock_mixin.subagents.runner_result.record_runner_result.return_value = mock_result

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
        mock_mixin.subagents.runner_result.record_runner_result.assert_called_once()
        call_params = mock_mixin.subagents.runner_result.record_runner_result.call_args[0][0]
        assert call_params.dry_run is True

    def test_run_subagent_with_attempt_id(self, mock_mixin: SimpleAgentSubagentMixin) -> None:
        """测试带 attempt_id 的 run。"""
        mock_mixin.subagents = MagicMock()
        mock_mixin.subagents.lifecycle.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id="attempt-123"
        )
        mock_mixin.subagents.runner_context.write_execution_context.return_value = MagicMock(
            allowed_tools=[],
            write_boundary={},
            goal="goal",
        )
        mock_result = MagicMock()
        mock_result.run_id = "run-id"
        mock_mixin.subagents.runner_result.record_runner_result.return_value = mock_result

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
        mock_mixin.subagents.runtime_db = None
        mock_mixin.subagents.lifecycle.prepare_runner_attempt.return_value = MagicMock(
            runner_active_attempt_id=""
        )
        mock_mixin.subagents.runner_context.write_execution_context.return_value = MagicMock(
            allowed_tools=[],
            write_boundary={},
            goal="goal",
        )
        mock_mixin.subagents.channel_probe.probe_channel.return_value = MagicMock(channel_status="BROKEN")
        mock_result = MagicMock()
        mock_result.run_id = "test"
        mock_mixin.subagents.runner_result.record_runner_result.return_value = mock_result

        with patch(
            "agent_py_agent.agent.agent_core.subagent_mixin._build_subagent_runner_prompt"
        ) as mock_prompt_builder:
            mock_prompt_builder.return_value = "built prompt"

            result = mock_mixin.run_subagent(
                run_id="test",
                dry_run=False,
                probe=True,
            )

        mock_mixin.subagents.runner_result.record_runner_result.assert_called_once()
        call_params = mock_mixin.subagents.runner_result.record_runner_result.call_args[0][0]
        assert call_params.status == "CHANNEL_ERROR"
        assert call_params.ok is False


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
        mock_mixin.subagents.parent_planner.make_parent_planner_record.return_value = mock_record
        mock_mixin.subagents.parent_planner.build_parent_planner_report.return_value = MagicMock()
        mock_mixin.subagents.parent_planner.write_parent_planner_report.return_value = None

        with patch("agent_py_agent.agent.agent_core.subagent_mixin._build_parent_planner_state") as mock_state:
            mock_state.return_value = {
                "gate": {"needs_planner": 0},
                "tasks": [],
            }
            from agent_py_agent.agent.agent_core.orchestration.dispatch.params import (
                DispatchExecutionPlan,
            )
            from agent_py_agent.agent.agent_core.subagent.params import (
                RunParentPlannerParams,
            )

            result = mock_mixin.run_parent_planner(
                RunParentPlannerParams(
                    router=MagicMock(),
                    capability_config=None,
                    execution_plan=DispatchExecutionPlan(
                        preview_only=True,
                        mutate_state=False,
                        start_runners=False,
                    ),
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

        with patch("agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot") as mock_write:
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

        with patch("agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot") as mock_write:
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

        with patch("agent_py_agent.agent.agent_core.subagent_mixin.write_recovery_snapshot") as mock_write:
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
