"""runner_dispatch.py 单元测试。

测试 runner 任务分配、并发控制、超时处理等核心功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRunnerMaxAttempts:
    """测试 _runner_max_attempts() 函数。"""

    def test_auto_policy_returns_2(self):
        """auto 策略返回 2 次尝试。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("auto") == 2
        assert _runner_max_attempts("Auto") == 2
        assert _runner_max_attempts("AUTO") == 2

    def test_empty_policy_returns_2(self):
        """空策略返回 2 次尝试。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("") == 2
        assert _runner_max_attempts(None) == 2

    def test_off_policy_returns_1(self):
        """off 策略返回 1 次尝试（不重试）。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("off") == 1
        assert _runner_max_attempts("none") == 1
        assert _runner_max_attempts("disabled") == 1

    def test_numeric_policy_returns_value(self):
        """数字策略返回对应次数。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("3") == 3
        assert _runner_max_attempts(3) == 3
        assert _runner_max_attempts(1) == 1

    def test_invalid_policy_returns_2(self):
        """无效策略默认返回 2。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("invalid") == 2
        assert _runner_max_attempts("abc") == 2


class TestRunnerFailureType:
    """测试 _runner_failure_type() 函数。"""

    def test_normalizes_failure_type(self):
        """验证 failure_type 被标准化为小写。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = "TIMEOUT"

        assert _runner_failure_type(mock_task) == "timeout"

    def test_strips_whitespace(self):
        """验证前后空格被去除。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = "  runner_error  "

        assert _runner_failure_type(mock_task) == "runner_error"

    def test_empty_failure_type(self):
        """空 failure_type 返回空字符串。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = ""

        assert _runner_failure_type(mock_task) == ""


class TestResolveRunnerConcurrency:
    """测试 _resolve_runner_concurrency() 函数。"""

    def test_auto_returns_1(self):
        """auto 策略返回 1，避免默认并发消耗 API。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency("auto", 5) == 1
        assert _resolve_runner_concurrency("", 5) == 1

    def test_zero_job_count_returns_0(self):
        """job_count 为 0 时返回 0。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(2, 0) == 0

    def test_negative_job_count_returns_0(self):
        """负数 job_count 返回 0。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(2, -1) == 0

    def test_numeric_value(self):
        """数字值直接转换。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(3, 5) == 3

    def test_capped_at_job_count(self):
        """并发数不超过 job_count。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(10, 3) == 3


class TestResolveRunnerStartRate:
    """测试 _resolve_runner_start_rate() 函数。"""

    def test_auto_returns_job_count(self):
        """auto 策略返回全部 job_count。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate("auto", 5) == 5

    def test_zero_job_count_returns_0(self):
        """job_count 为 0 时返回 0。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(3, 0) == 0

    def test_numeric_value(self):
        """数字值直接转换。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(2, 10) == 2

    def test_capped_at_job_count(self):
        """启动率不超过 job_count。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(20, 5) == 5


class TestResolveRunnerTimeoutSeconds:
    """测试 _resolve_runner_timeout_seconds() 函数。"""

    def test_off_returns_zero(self):
        """off/disabled 等禁用超时。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds("off") == 0.0
        assert _resolve_runner_timeout_seconds("disabled") == 0.0
        assert _resolve_runner_timeout_seconds("none") == 0.0

    def test_auto_returns_zero(self):
        """auto 默认不启用超时。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds("auto") == 0.0

    def test_numeric_seconds(self):
        """数字值转换为秒数。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds(30.5) == 30.5
        assert _resolve_runner_timeout_seconds("60") == 60.0

    def test_negative_returns_zero(self):
        """负数返回 0。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds(-10) == 0.0


class TestRunnerTaskTimeout:
    """测试 runner 自动超时计算。"""

    def _timeout_config(self, runner_timeout_seconds: str = "auto"):
        """构造动态超时配置。"""

        config = MagicMock()
        config.runner_timeout_seconds = runner_timeout_seconds
        config.dynamic_timeout_safety_margin = 2.0
        config.dynamic_timeout_min = 30
        config.dynamic_timeout_max = 600
        config.model_speed_profile_path = ""
        return config

    def test_off_runner_timeout_returns_no_limit(self):
        """用户配置 off/none/disabled 时，runner 不套超时墙。"""
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

        task = MagicMock()
        task.attributes = {}
        task.goal = "实现购物网站 demo，包含注册、登录、购物车和下单。"
        task.plan = ["write files", "verify behavior"]
        task.role = "worker"
        task.allowed_tools = ["read_file", "write_file"]

        assert get_task_timeout(task, 0.0, self._timeout_config("off")) == 0.0

    # LLM: runner timeout off must override stale adaptive timeout attributes after a failed run.
    # 函数用途: 确认用户关闭 runner 超时后，旧任务里的 dynamic_timeout_seconds 不会继续制造隐藏超时墙。
    def test_off_runner_timeout_ignores_dynamic_timeout_attribute(self):
        """用户配置 off 时，失败后遗留的动态超时也不能重新启用超时。"""
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

        task = MagicMock()
        task.attributes = {"dynamic_timeout_seconds": 300.0}
        task.goal = "恢复一个刚刚 timeout 的子代理任务。"
        task.plan = ["读取 continue packet", "继续执行"]
        task.role = "worker"
        task.allowed_tools = ["read_file", "write_file"]

        assert get_task_timeout(task, 0.0, self._timeout_config("off")) == 0.0

    def test_static_runner_timeout_still_overrides_no_limit_config(self):
        """用户显式数字超时时，仍按数字超时执行。"""
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

        task = MagicMock()
        task.attributes = {}
        task.goal = "实现一个小改动。"
        task.plan = []
        task.role = "worker"
        task.allowed_tools = ["write_file"]

        assert get_task_timeout(task, 45.0, self._timeout_config("off")) == 45.0


class TestIsDispatchRunnerCandidate:
    """测试 _is_dispatch_runner_candidate() 函数。"""

    # LLM: active RUNNING attempts must not be selected again by ordinary dispatch.
    # 函数用途: 防止同一个 run 在前一次模型回合未结束时被 dispatch 再次启动，造成 stale result。
    def test_running_task_with_active_attempt_is_not_runner_candidate(self):
        """RUNNING 且已有 active attempt 时，不能被普通 dispatch 重入执行。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.runner_active_attempt_id = "attempt-active"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task, runner_max_attempts=2) is False

    def test_done_status_not_candidate(self):
        """已完成任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "DONE"
        mock_task.verification_status = "VERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_channel_broken_not_candidate(self):
        """通道损坏的任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "BROKEN"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_open_capability_request_not_candidate(self):
        """有待处理的 capability_requests 不是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_request = MagicMock()
        mock_request.status = "OPEN"

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = [mock_request]
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_running_status_without_active_attempt_is_not_candidate(self):
        """RUNNING 即便没有 active attempt，也不能被普通 dispatch 重入执行。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_planning_status_is_candidate(self):
        """PLANNING 状态是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "PLANNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is True

    def test_failed_with_retryable_reason(self):
        """可重试失败类型的 FAILED 任务是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "FAILED"
        mock_task.failure_type = "runner_error"
        mock_task.runner_attempts = 0
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task, runner_max_attempts=2) is True

    def test_non_retryable_failure_not_candidate(self):
        """不可重试失败类型的任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "FAILED"
        mock_task.failure_type = "capability_request"
        mock_task.runner_attempts = 0
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []
        mock_task.capability_grants = []

        # capability_request 在 capability_grants 为空时不可重试
        assert _is_dispatch_runner_candidate(mock_task, runner_max_attempts=2) is False


class TestDispatchRunnerCandidates:
    """测试 _dispatch_runner_candidates() 函数。"""

    def _runner_task(self, run_id: str, role: str, created_at: float = 1.0):
        """构造可调度 runner 候选，便于测试角色排序。"""

        task = MagicMock()
        task.id = run_id
        task.role = role
        task.status = "PLANNING"
        task.verification_status = "PENDING"
        task.channel_status = "OK"
        task.capability_requests = []
        task.capability_gaps = []
        task.created_at = created_at
        task.updated_at = created_at
        return task

    def test_zero_max_runners_returns_empty(self):
        """max_runners 为 0 返回空列表。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates

        mock_task = MagicMock()
        result = _dispatch_runner_candidates([mock_task], max_runners=0)
        assert result == []

    def test_returns_up_to_max_runners(self):
        """返回最多 max_runners 个候选。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates

        mock_task1 = MagicMock()
        mock_task1.status = "PLANNING"
        mock_task1.verification_status = "PENDING"
        mock_task1.channel_status = "OK"
        mock_task1.capability_requests = []
        mock_task1.capability_gaps = []

        mock_task2 = MagicMock()
        mock_task2.status = "PLANNING"
        mock_task2.verification_status = "PENDING"
        mock_task2.channel_status = "OK"
        mock_task2.capability_requests = []
        mock_task2.capability_gaps = []

        result = _dispatch_runner_candidates([mock_task1, mock_task2], max_runners=1)
        assert len(result) == 1

    def test_role_phase_order_is_applied_before_runner_limit(self):
        """worker 先于找错/测试/验收，且排序发生在 max_runners 截断前。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates

        tasks = [
            self._runner_task("accept", "acceptor", created_at=1.0),
            self._runner_task("bug", "bug_finder", created_at=2.0),
            self._runner_task("test", "tester", created_at=3.0),
            self._runner_task("work", "worker", created_at=4.0),
        ]

        result = _dispatch_runner_candidates(tasks, max_runners=1)

        assert [task.id for task in result] == ["work"]


class TestLimitItems:
    """测试 _limit_items() 函数。"""

    def test_zero_limit_returns_all(self):
        """limit 为 0 返回全部。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _limit_items

        items = [1, 2, 3, 4, 5]
        result = _limit_items(items, 0)
        assert result == items

    def test_positive_limit_truncates(self):
        """正数 limit 截断列表。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _limit_items

        items = [1, 2, 3, 4, 5]
        result = _limit_items(items, 3)
        assert result == [1, 2, 3]

    def test_returns_copy(self):
        """返回列表副本，不修改原列表。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _limit_items

        items = [1, 2, 3]
        result = _limit_items(items, 2)
        assert result != items
        assert items == [1, 2, 3]


class TestRetryableRunnerFailureTypes:
    """测试 RETRYABLE_RUNNER_FAILURE_TYPES 常量。"""

    def test_contains_expected_types(self):
        """验证包含预期的可重试失败类型。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import RETRYABLE_RUNNER_FAILURE_TYPES

        assert "runner_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "structured_output_parse_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "tool_result_missing" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "api_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "runner_timeout" in RETRYABLE_RUNNER_FAILURE_TYPES
