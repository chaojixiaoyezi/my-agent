"""agent_core.runner.dispatch 候选筛选单元测试。

测试 runner 任务分配、并发控制、超时处理等核心功能。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.agent_core.runner.dispatch import RunnerCandidatePolicy


class TestIsDispatchRunnerCandidate:
    """测试 _is_dispatch_runner_candidate() 函数。"""

    def test_running_task_with_active_attempt_is_not_runner_candidate(self):
        """RUNNING 且已有 active attempt 时，不能被普通 dispatch 重入执行。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.runner_active_attempt_id = "attempt-active"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task, policy=RunnerCandidatePolicy(runner_max_attempts=2)) is False

    def test_done_status_not_candidate(self):
        """已完成任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "DONE"
        mock_task.verification_status = "VERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_channel_broken_not_candidate(self):
        """通道损坏的任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "BROKEN"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_open_capability_request_not_candidate(self):
        """有待处理的 capability_requests 不是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

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
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_planning_status_is_candidate(self):
        """PLANNING 状态是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "PLANNING"
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is True

    def test_pending_stalled_orphan_is_candidate(self):
        """PENDING 停滞孤儿(runner 被 orphan 回收、background_start 已 terminated)应被续派。

        多子代理任务死循环卡死的核心修复:原先 candidate 兜底只认 PLANNING,被 background 启动后
        进程被回收留下的 PENDING 孤儿永不续派。现在 PENDING 与 PLANNING 同等可派(对齐 can_dispatch)。
        """
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "PENDING"
        mock_task.runner_active_attempt_id = ""  # 无活跃 attempt
        mock_task.attributes = {"background_start": {"status": "terminated"}}  # 进程已被回收
        mock_task.verification_status = "UNVERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is True

    def test_pending_launching_in_progress_not_candidate(self):
        """PENDING 但 runner 正在启动中(background_start=running)不重复派——不和在途 runner 撞车。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "PENDING"
        mock_task.runner_active_attempt_id = ""
        mock_task.attributes = {"background_start": {"status": "running"}}  # 正在跑
        mock_task.verification_status = "UNVERIFIED"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task) is False

    def test_failed_with_retryable_reason(self):
        """可重试失败类型的 FAILED 任务是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        mock_task = MagicMock()
        mock_task.status = "FAILED"
        mock_task.failure_type = "runner_error"
        mock_task.runner_attempts = 0
        mock_task.verification_status = "PENDING"
        mock_task.channel_status = "OK"
        mock_task.capability_requests = []
        mock_task.capability_gaps = []

        assert _is_dispatch_runner_candidate(mock_task, policy=RunnerCandidatePolicy(runner_max_attempts=2)) is True

    def test_non_retryable_failure_not_candidate(self):
        """不可重试失败类型的任务不是候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

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
        assert _is_dispatch_runner_candidate(mock_task, policy=RunnerCandidatePolicy(runner_max_attempts=2)) is False

    def test_provider_timeout_blocked_task_is_retry_candidate(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

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

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=2)) is True


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
        from agent_py_agent.agent.agent_core.runner.dispatch import _dispatch_runner_candidates

        mock_task = MagicMock()
        result = _dispatch_runner_candidates([mock_task], max_runners=0)
        assert result == []

    def test_returns_up_to_max_runners(self):
        """返回最多 max_runners 个候选。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _dispatch_runner_candidates

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
        from agent_py_agent.agent.agent_core.runner.dispatch import _dispatch_runner_candidates

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
        from agent_py_agent.agent.agent_core.runner.dispatch import _limit_items

        items = [1, 2, 3, 4, 5]
        result = _limit_items(items, 0)
        assert result == items

    def test_positive_limit_truncates(self):
        """正数 limit 截断列表。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _limit_items

        items = [1, 2, 3, 4, 5]
        result = _limit_items(items, 3)
        assert result == [1, 2, 3]

    def test_returns_copy(self):
        """返回列表副本，不修改原列表。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _limit_items

        items = [1, 2, 3]
        result = _limit_items(items, 2)
        assert result != items
        assert items == [1, 2, 3]


class TestRetryableRunnerFailureTypes:
    """测试 RETRYABLE_RUNNER_FAILURE_TYPES 常量。"""

    def test_contains_expected_types(self):
        """验证包含预期的可重试失败类型。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import RETRYABLE_RUNNER_FAILURE_TYPES

        assert "runner_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "structured_output_parse_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "tool_result_missing" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "api_error" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "provider_timeout" in RETRYABLE_RUNNER_FAILURE_TYPES
        assert "runner_timeout" in RETRYABLE_RUNNER_FAILURE_TYPES


class TestProviderSupplyRedispatch:
    """临时供应错(模型 429 断供,failure_type=transient_error)的独立重派上限。

    真机实锤:默认闸(runner_failure_retry_limit=2 + same_run_redispatch_limit=1)下,
    几分钟的额度断供把重派预算烧穿,任务永久卡 BLOCKED,额度恢复也不复活(1.10 死透)。
    供应断供是环境故障不是任务失败,走 provider_transient_redispatch_limit(默认 8)。
    """

    @staticmethod
    def _blocked_task(failure_type: str, attempts: int):
        return SimpleNamespace(
            status="BLOCKED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            capability_grants=[],
            failure_type=failure_type,
            runner_attempts=attempts,
        )

    @staticmethod
    def _fixed_supply_limit(monkeypatch, limit: int) -> None:
        from agent_py_agent.agent.agent_core.runner import dispatch

        monkeypatch.setattr(
            dispatch,
            "runtime_guard_int",
            lambda key, default=0, **kwargs: limit
            if key == "provider_transient_redispatch_limit"
            else default,
        )

    def test_transient_outage_task_stays_redispatchable_beyond_default_gates(self, monkeypatch):
        """撤修复即 FAIL:429 断供任务在默认闸(2/1)下第 2 次尝试后就永久失格。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        self._fixed_supply_limit(monkeypatch, 8)
        task = self._blocked_task("transient_error", attempts=2)
        policy = RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=1)

        assert _is_dispatch_runner_candidate(task, policy=policy) is True

    def test_transient_outage_redispatch_has_accountable_upper_bound(self, monkeypatch):
        """供应类重派有上限可核算:attempts 超 provider_transient_redispatch_limit 即失格,不无限刷。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        self._fixed_supply_limit(monkeypatch, 8)
        policy = RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=1)

        assert _is_dispatch_runner_candidate(self._blocked_task("transient_error", 8), policy=policy) is True
        assert _is_dispatch_runner_candidate(self._blocked_task("transient_error", 9), policy=policy) is False

    def test_non_supply_failure_keeps_original_gates(self, monkeypatch):
        """不回归:普通失败(runner_error)仍走原闸,attempts=2 在 same_run_redispatch_limit=1 下失格。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        self._fixed_supply_limit(monkeypatch, 8)
        task = self._blocked_task("runner_error", attempts=2)
        policy = RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=1)

        assert _is_dispatch_runner_candidate(task, policy=policy) is False

    def test_zero_limit_disables_supply_privilege(self, monkeypatch):
        """provider_transient_redispatch_limit=0 关闭特权:供应类失败回归与普通失败同闸。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        self._fixed_supply_limit(monkeypatch, 0)
        task = self._blocked_task("transient_error", attempts=2)
        policy = RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=1)

        assert _is_dispatch_runner_candidate(task, policy=policy) is False

    def test_explicit_no_retry_policy_does_not_kill_supply_outage_recovery(self, monkeypatch):
        """runner_max_attempts=1(不因任务失败重试)不掐死供应断供恢复:断供不是任务失败。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        self._fixed_supply_limit(monkeypatch, 8)
        task = self._blocked_task("transient_error", attempts=1)
        policy = RunnerCandidatePolicy(runner_max_attempts=1, same_run_redispatch_limit=1)

        assert _is_dispatch_runner_candidate(task, policy=policy) is True
