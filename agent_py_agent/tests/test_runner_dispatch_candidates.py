"""runner_dispatch.py 单元测试。

测试 runner 任务分配、并发控制、超时处理等核心功能。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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

    # LLM: Provider timeouts are transient model-service failures and must enter bounded runner retry.
    # 函数用途: 确认真实模型请求超时后的 BLOCKED runner 会被下一轮 dispatch 选中重试，而不是只做 classify_blocker。
    def test_provider_timeout_blocked_task_is_retry_candidate(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="BLOCKED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            capability_grants=[],
            failure_type="provider_timeout",
            runner_attempts=1,
        )

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=2) is True


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

    def test_runner_limit_preserves_candidate_order_without_hidden_role_phase(self):
        """runner 不再按角色阶段重排；父代理要顺序时显式控制。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _dispatch_runner_candidates

        tasks = [
            self._runner_task("accept", "bug_finder", created_at=1.0),
            self._runner_task("bug", "bug_finder", created_at=2.0),
            self._runner_task("test", "tester", created_at=3.0),
            self._runner_task("work", "worker", created_at=4.0),
        ]

        result = _dispatch_runner_candidates(tasks, max_runners=1)

        assert [task.id for task in result] == ["accept"]


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
        assert "provider_timeout" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "runner_timeout" in RETRYABLE_RUNNER_FAILURE_TYPES
