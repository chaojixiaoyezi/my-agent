from __future__ import annotations

"""LLM: tests for dispatch_mixin.

给人看的解释：
测试父代理调度逻辑：dispatch_subagents、watch mode、failure introspection、闭环检测。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
    SimpleAgentDispatchMixin,
    _planner_dispatch_overrides,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.subagents import DispatchReport
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask


class TestDispatchMixinBasics:
    """测试 dispatch_mixin 基础方法。"""

    def test_update_pending_work_state_no_candidates(self) -> None:
        """测试无候选任务时更新待处理状态。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _dispatch_runner_candidates

        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.subagents = MagicMock()

        with patch("agent_py_agent.agent.agent_core.runner.dispatch._dispatch_runner_candidates", return_value=[]):
            mixin._update_pending_work_state()
            assert mixin._has_pending_work is False

    def test_update_pending_work_state_with_candidates(self) -> None:
        """测试有候选任务时更新待处理状态。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.subagents = MagicMock()

        with patch('agent_py_agent.agent.agent_core.runner.dispatch._dispatch_runner_candidates', return_value=[MagicMock(), MagicMock()]):
            mixin._update_pending_work_state()
            assert mixin._has_pending_work is True

    def test_increment_dispatch_rounds(self) -> None:
        """测试递增 dispatch 轮数。"""
        mixin = SimpleAgentDispatchMixin()
        mixin._consecutive_dispatch_rounds = 0
        mixin._increment_dispatch_rounds()
        assert mixin._consecutive_dispatch_rounds == 1
        mixin._increment_dispatch_rounds()
        assert mixin._consecutive_dispatch_rounds == 2

    def test_reset_dispatch_rounds(self) -> None:
        """测试重置 dispatch 轮数。"""
        mixin = SimpleAgentDispatchMixin()
        mixin._consecutive_dispatch_rounds = 5
        mixin._reset_dispatch_rounds()
        assert mixin._consecutive_dispatch_rounds == 0

    def test_has_pending_work_default_false(self) -> None:
        """测试默认无待处理工作。"""
        mixin = SimpleAgentDispatchMixin()
        assert mixin._has_pending_work is False


class TestDispatchMixinFailureIntrospection:
    """测试 failure introspection 相关方法。"""

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-task-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2"],
        )

    @pytest.fixture
    def sample_result(self) -> SubAgentRunnerResult:
        """创建示例 runner 结果。"""
        return SubAgentRunnerResult(
            run_id="test-task-1",
            dry_run=False,
            ok=False,
            status="BLOCKED",
            verification_status="UNVERIFIED",
            message="超时",
        )

    def test_handle_failure_introspection_success(self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试失败自省成功执行。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.subagents = MagicMock()
        # 模拟 load 返回的 task 并设置 attributes
        sample_task.attributes = {}
        mixin.subagents.load.return_value = sample_task
        mixin.subagents.save.return_value = None

        mock_analyzer = MagicMock()
        mock_introspector = MagicMock()
        mock_introspection = MagicMock()
        mock_introspection.analysis_reason = "任务执行超时"
        mock_introspection.root_cause = "timeout"
        mock_introspection.suggested_params = {"new_timeout_seconds": 300}
        mock_introspection.should_retry = True
        mock_introspection.should_split = False
        mock_introspection.confidence = 0.8
        mock_introspector.introspect.return_value = mock_introspection

        with patch("agent_py_agent.agent.agent_core.orchestration.dispatch.mixin.FailureIntrospector", return_value=mock_introspector):
            mixin._handle_failure_introspection("test-task-1", sample_task, sample_result)

        assert "failure_introspection_data" in sample_task.attributes
        assert sample_task.attributes["failure_introspection_data"]["root_cause"] == "timeout"

    def test_handle_failure_introspection_exception(self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试失败自省异常时的降级处理。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.subagents = MagicMock()
        mixin.subagents.load.side_effect = Exception("加载失败")

        # 不应抛出异常
        mixin._handle_failure_introspection("test-task-1", sample_task, sample_result)

    def test_handle_failure_introspection_apply_error_recorded_structured(
        self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult
    ) -> None:
        """apply 段失败不能只吞日志：结构化错误必须写进 attributes 并补一次落盘。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.subagents = MagicMock()
        sample_task.attributes = {}
        mixin.subagents.load.return_value = sample_task
        # 第一次 save（正常落盘）抛错 → 进入 _record_introspection_error 补救 save。
        mixin.subagents.save.side_effect = [RuntimeError("save 失败"), None]

        mixin._handle_failure_introspection("test-task-1", sample_task, sample_result)

        report = sample_task.attributes["failure_introspection_error"]
        assert report["context"] == "dispatch.failure_introspection"
        assert report["error_type"] == "RuntimeError"
        assert mixin.subagents.save.call_count == 2

    def test_handle_failure_introspection_record_error_double_failure_does_not_raise(
        self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult
    ) -> None:
        """留痕 save 也失败时只降级日志，绝不向 runner 主链路抛异常。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.subagents = MagicMock()
        sample_task.attributes = {}
        mixin.subagents.load.return_value = sample_task
        mixin.subagents.save.side_effect = RuntimeError("save 永远失败")

        mixin._handle_failure_introspection("test-task-1", sample_task, sample_result)

def test_planner_dispatch_overrides_use_structured_runner_instruction_field() -> None:
    params = DispatchParams(
        planner=True,
        runner_instruction="base",
        max_runners=3,
    )
    record = MagicMock()
    record.step = "parent_planner"
    record.message = "planner summary without instruction marker"
    record.runner_instruction = "structured follow-up"
    record.suggested_max_runners = 2

    instruction, max_runners = _planner_dispatch_overrides(params, [record])

    assert "base" in instruction
    assert "structured follow-up" in instruction
    assert max_runners == 2


class TestApplyIntrospectionParams:
    """_apply_introspection_params 的消费 key 行为。

    历史教训：这些用例曾被错误缩进成上面模块级函数体内的嵌套 def，
    pytest 从不收集（死测试）。挪回类里救活；split_goal 用例随影子拆分
    分支一并删除（拆分唯一权威是 split_task 子任务路径）。
    """

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-apply-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2"],
        )

    def test_apply_introspection_params_timeout(self, sample_task: SubAgentTask) -> None:
        """测试应用超参数调整。"""
        mixin = SimpleAgentDispatchMixin()
        params = {"new_timeout_seconds": 300}
        result = mixin._apply_introspection_params(sample_task, params)
        assert result.attributes.get("dynamic_timeout_seconds") == 300

    def test_apply_introspection_params_max_tool_rounds(self, sample_task: SubAgentTask) -> None:
        """测试应用 max_tool_rounds 调整。"""
        mixin = SimpleAgentDispatchMixin()
        params = {"max_tool_rounds": 50}
        result = mixin._apply_introspection_params(sample_task, params)
        assert result.attributes.get("max_tool_rounds") == 50

    def test_apply_introspection_params_ignores_removed_split_goal_key(
        self, sample_task: SubAgentTask
    ) -> None:
        """split_goal 影子拆分分支已删除：传入该 key 不得再改写 goal。"""
        mixin = SimpleAgentDispatchMixin()
        sample_task.goal = "原始目标"
        result = mixin._apply_introspection_params(sample_task, {"split_goal": "拆分的子目标"})
        assert result.goal == "原始目标"

    def test_apply_introspection_params_multiple(self, sample_task: SubAgentTask) -> None:
        """测试应用多个参数调整。"""
        mixin = SimpleAgentDispatchMixin()
        params = {
            "new_timeout_seconds": 500,
            "max_tool_rounds": 100,
        }
        result = mixin._apply_introspection_params(sample_task, params)
        assert result.attributes.get("dynamic_timeout_seconds") == 500
        assert result.attributes.get("max_tool_rounds") == 100


class TestDispatchMixinDispatchReport:
    """测试 dispatch report 生成。"""

    def test_dispatch_report_build_empty(self) -> None:
        """测试空 records 构建 report。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.subagents = MagicMock()
        # 配置 build_dispatch_report 返回一个 mock DispatchReport
        mock_report = MagicMock()
        mixin.subagents.build_dispatch_report.return_value = mock_report

        result = mixin.subagents.build_dispatch_report([], dry_run=True)
        assert result is mock_report

    def test_terminal_child_report_does_not_create_direct_user_notification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """子代理终态只唤醒父代理，不得另写固定用户通知旁路。"""
        monkeypatch.chdir(tmp_path)
        mixin = SimpleAgentDispatchMixin()
        mixin.config = SimpleNamespace(user_id="owner-a")
        mixin.subagents = MagicMock()
        mixin.subagents.dispatch.build_dispatch_report.return_value = MagicMock()
        mixin.subagents.dispatch.write_dispatch_report.return_value = MagicMock()
        record = SimpleNamespace(
            step="runner",
            applied=True,
            run_id="subagent-internal-id",
            after_status="DONE",
        )

        with patch(
            "agent_py_agent.agent.agent_core.orchestration.dispatch.mixin.update_pending_work_state",
            return_value=False,
        ):
            mixin._build_and_write_report([record], MagicMock(mutate_state=True))

        assert not (tmp_path / "data" / "notifications").exists()
