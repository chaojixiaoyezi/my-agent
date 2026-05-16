from __future__ import annotations

"""LLM: tests for dispatch_mixin.

给人看的解释：
测试父代理调度逻辑：dispatch_subagents、watch mode、failure introspection、闭环检测。
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin
from agent_py_agent.agent.agent_core.services.notification_service import notify_completed_tasks
from agent_py_agent.agent.subagent import DispatchReport
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask


class TestDispatchMixinBasics:
    """测试 dispatch_mixin 基础方法。"""

    def test_update_pending_work_state_no_candidates(self) -> None:
        """测试无候选任务时更新待处理状态。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates

        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.subagents = MagicMock()

        with patch("agent_py_agent.agent.agent_core.runner_dispatch._dispatch_runner_candidates", return_value=[]):
            mixin._update_pending_work_state()
            assert mixin._has_pending_work is False

    def test_update_pending_work_state_with_candidates(self) -> None:
        """测试有候选任务时更新待处理状态。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.subagents = MagicMock()

        with patch('agent_py_agent.agent.agent_core.runner_dispatch._dispatch_runner_candidates', return_value=[MagicMock(), MagicMock()]):
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
        mixin.run = MagicMock()  # FailureIntrospector needs agent.run

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

        with patch('agent_py_agent.agent.agent_core.failure_introspector.FailureIntrospector', return_value=mock_introspector):
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

    def test_apply_introspection_params_split_goal(self, sample_task: SubAgentTask) -> None:
        """测试应用 goal 拆分。"""
        mixin = SimpleAgentDispatchMixin()
        sample_task.goal = "原始目标"
        params = {"split_goal": "拆分的子目标"}
        result = mixin._apply_introspection_params(sample_task, params)
        assert "拆分的子目标" in result.goal

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


class TestDispatchMixinNotifyCompleted:
    """测试任务完成通知。"""

    def test_notify_completed_disabled(self) -> None:
        """测试通知被禁用时不发送。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.config.notification_enabled = False

        records = [MagicMock(step="runner", run_id="task-1", applied=True)]
        mixin._notify_completed_tasks(records)

    def test_notify_completed_no_records(self) -> None:
        """测试空记录列表。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.config.notification_enabled = True

        mixin._notify_completed_tasks([])

    def test_notify_completed_non_runner_record(self) -> None:
        """测试非 runner 记录。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.config.notification_enabled = True

        records = [MagicMock(step="planner", run_id="task-1", applied=True)]
        mixin._notify_completed_tasks(records)

    def test_notify_completed_not_applied(self) -> None:
        """测试未 apply 的记录。"""
        mixin = SimpleAgentDispatchMixin()
        mixin.config = MagicMock()
        mixin.config.notification_enabled = True

        records = [MagicMock(step="runner", run_id="task-1", applied=False)]
        mixin._notify_completed_tasks(records)

    def test_completed_task_notification_reaches_chat_channel(self, tmp_path: Path) -> None:
        """测试完成通知能真正进入可用 chat 通道。"""
        config = _notification_test_config(tmp_path)
        _write_online_chat_session(config, "admin")
        agent = _notification_test_agent(config)
        record = SimpleNamespace(step="runner", run_id="run-1", applied=True, after_status="DONE")

        notify_completed_tasks(agent, [record])

        payload = _single_notification_payload(tmp_path)
        assert payload["status"] == "delivered"
        assert payload["delivery_channel"] == "chat"

    def test_completed_task_notification_delivery_exception_is_recorded(self, tmp_path: Path) -> None:
        """测试投递异常不会打断收尾，但会把通知标成 failed。"""
        config = _notification_test_config(tmp_path)
        _write_online_chat_session(config, "admin")
        agent = _notification_test_agent(config)
        record = SimpleNamespace(step="runner", run_id="run-1", applied=True, after_status="DONE")

        with patch(
            "agent_py_agent.agent.notification.NotificationRouter.deliver",
            side_effect=TimeoutError("gateway timeout"),
        ):
            notify_completed_tasks(agent, [record])

        payload = _single_notification_payload(tmp_path)
        assert payload["status"] == "failed"
        assert "gateway timeout" in str(payload["last_error"])


# LLM: notification test helpers keep completion-notification fixtures realistic without reaching the real user home.
# 函数用途: 生成通知测试配置，把通知、会话和适配器目录都限制在 pytest 临时目录里。
def _notification_test_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        notification_enabled=True,
        notification_store_path=str(tmp_path / "notifications"),
        session_workspace=str(tmp_path / "sessions"),
        adapter_workspace=str(tmp_path / "adapters"),
        notification_channel_timeout_seconds=300,
        user_id="admin",
    )


# LLM: this fixture mirrors a finished subagent task while avoiding the full manager stack.
# 函数用途: 构造最小 agent 对象，让 notify_completed_tasks 能加载 DONE 任务并创建完成通知。
def _notification_test_agent(config: SimpleNamespace) -> SimpleNamespace:
    task = SimpleNamespace(
        status="DONE",
        root_id="root-1",
        goal="完成任务",
        runner_attempts=1,
        last_active_channel="chat",
    )
    subagents = MagicMock()
    subagents.load.return_value = task
    return SimpleNamespace(config=config, subagents=subagents)


# LLM: chat delivery depends on a fresh session heartbeat, so tests write the same file the router reads.
# 函数用途: 写入在线 chat 会话，让通知路由器选择 chat 作为真实可投递通道。
def _write_online_chat_session(config: SimpleNamespace, user_id: str) -> None:
    session_dir = Path(config.session_workspace) / "session-1"
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {"user_id": user_id, "last_active_channel": "chat", "updated_at": time.time()}
    (session_dir / "session.json").write_text(json.dumps(payload), encoding="utf-8")


# LLM: notification assertions read the persisted JSON contract instead of private objects.
# 函数用途: 读取测试生成的唯一通知文件，方便断言状态、渠道和后续兼容字段。
def _single_notification_payload(tmp_path: Path) -> dict[str, object]:
    paths = list((tmp_path / "notifications").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


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
