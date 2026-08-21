"""agent_core.runner.dispatch 单元测试。

测试 runner 任务分配、并发控制、超时处理等核心功能。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.agent_core.runner.dispatch import RunnerCandidatePolicy


def test_execute_runner_uses_worker_even_when_timeout_disabled(monkeypatch, tmp_path):
    from agent_py_agent.agent.agent_core.runner.gate import SingleRunnerParams, run_single_runner

    calls: list[object] = []

    def fake_worker(params):
        calls.append(params)
        return SimpleNamespace(ok=True, run_id=params.run_id, status="DONE", verification_status="VERIFIED")

    class ParentAgent:
        config = SimpleNamespace()
        root = tmp_path
        local_store = None

        def run_subagent(self, params):  # pragma: no cover - should not be called for execute=True
            raise AssertionError("execute runner should not run on the parent agent object")

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runner.dispatch._run_subagent_worker",
        fake_worker,
    )

    result = run_single_runner(
        SingleRunnerParams(
            agent=ParentAgent(),
            run_id="subagent-1",
            task_timeout=0.0,
            instruction="继续完成任务",
            start_runner=True,
            max_cards=0,
            probe=True,
            retry_reason="",
        )
    )

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0].timeout_seconds == 0.0
    assert calls[0].run_id == "subagent-1"


def test_runner_failure_reports_memory_injection_error(monkeypatch):
    from agent_py_agent.agent.agent_core.runner.gate import (
        RunnerFailureParams,
        handle_runner_failure,
    )

    def broken_push(*_args, **_kwargs):
        raise OSError("memory push unavailable")

    monkeypatch.setattr("agent_py_agent.agent.memory_push.push_relevant_memories", broken_push)
    introspections: list[str] = []
    agent = SimpleNamespace(
        _has_pending_work=False,
        _handle_failure_introspection=lambda run_id, _before, _result: introspections.append(run_id),
    )

    instruction = handle_runner_failure(
        RunnerFailureParams(
            agent=agent,
            run_id="runner-1",
            before=SimpleNamespace(goal="继续写报告"),
            result=SimpleNamespace(status="BLOCKED"),
            effective_instruction="继续推进",
        )
    )

    assert agent._has_pending_work is True
    assert introspections == ["runner-1"]
    assert "RUNNER_FAILURE_MEMORY_INJECTION_ERROR" in instruction
    assert "runner_failure.memory_injection" in instruction
    assert "memory push unavailable" in instruction


class TestRunnerMaxAttempts:
    """测试 _runner_max_attempts() 函数。"""

    def test_auto_policy_returns_2(self):
        """auto 策略返回 2 次尝试。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        assert _runner_max_attempts("auto") == 2
        assert _runner_max_attempts("Auto") == 2
        assert _runner_max_attempts("AUTO") == 2

    def test_empty_policy_returns_2(self):
        """空策略返回 2 次尝试。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        assert _runner_max_attempts("") == 2
        assert _runner_max_attempts(None) == 2

    def test_off_policy_returns_1(self):
        """只有 off/0 显式关闭补跑；旧别名不再改变机器语义。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        assert _runner_max_attempts("off") == 0
        assert _runner_max_attempts("0") == 0
        assert _runner_max_attempts("none") == 2
        assert _runner_max_attempts("disabled") == 2

    def test_numeric_policy_returns_value(self):
        """数字策略返回对应失败后重试次数。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        assert _runner_max_attempts("3") == 3
        assert _runner_max_attempts(3) == 3
        assert _runner_max_attempts(1) == 1
        assert _runner_max_attempts("0") == 0
        assert _runner_max_attempts(0) == 0

    def test_invalid_policy_returns_2(self):
        """无效策略默认返回 2。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts

        assert _runner_max_attempts("invalid") == 2
        assert _runner_max_attempts("abc") == 2

    def test_zero_policy_keeps_retry_candidate_unlimited(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=99,
        )

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=0, same_run_redispatch_limit=0)) is True

    def test_runner_failure_retry_limit_counts_retries_after_initial_attempt(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=2,
        )

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=0)) is True
        task.runner_attempts = 3
        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=2, same_run_redispatch_limit=0)) is False

    def test_same_run_redispatch_limit_blocks_repeating_same_run(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=2,
        )

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=3, same_run_redispatch_limit=1)) is False
        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=3, same_run_redispatch_limit=0)) is True


class TestRunnerFailureType:
    """测试 _runner_failure_type() 函数。"""

    def test_ignores_unknown_failure_type(self):
        """未知 failure_type 不进入 runner 重试策略。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = "TIMEOUT"

        assert _runner_failure_type(mock_task) == ""

    def test_strips_whitespace(self):
        """验证前后空格被去除。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = "  runner_error  "

        assert _runner_failure_type(mock_task) == "runner_error"

    def test_empty_failure_type(self):
        """空 failure_type 返回空字符串。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _runner_failure_type

        mock_task = MagicMock()
        mock_task.failure_type = ""

        assert _runner_failure_type(mock_task) == ""


class TestRunnerCandidateCapabilityGrant:
    """测试能力授权后的 blocked runner 能继续执行。"""

    def test_permission_blocked_with_grant_is_runner_candidate(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="BLOCKED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[SimpleNamespace(status="GRANTED")],
            capability_gaps=[],
            capability_grants=[SimpleNamespace(id="grant-write")],
            failure_type="permission_blocked",
            runner_attempts=1,
            current_step="等待父级授权写入 allowed_write_roots",
            result="",
            blockers=[],
        )

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=1)) is True

    def test_open_request_still_blocks_runner_candidate(self):
        from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="BLOCKED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[SimpleNamespace(status="OPEN")],
            capability_gaps=[],
            capability_grants=[SimpleNamespace(id="grant-write")],
            failure_type="permission_blocked",
            runner_attempts=1,
            current_step="等待父级授权写入 allowed_write_roots",
            result="",
            blockers=[],
        )

        assert _is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy(runner_max_attempts=2)) is False


class TestResolveRunnerConcurrency:
    """测试 _resolve_runner_concurrency() 函数。"""

    def test_auto_uses_bounded_job_count(self):
        """auto 策略按任务数并发，但受内部安全上限约束。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency("auto", 5) == 4
        assert _resolve_runner_concurrency("", 5) == 4
        assert _resolve_runner_concurrency("auto", 20) == 4

    def test_zero_job_count_returns_0(self):
        """job_count 为 0 时返回 0。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(2, 0) == 0

    def test_negative_job_count_returns_0(self):
        """负数 job_count 返回 0。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(2, -1) == 0

    def test_numeric_value(self):
        """数字值直接转换。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(3, 5) == 3

    def test_capped_at_job_count(self):
        """并发数不超过 job_count。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency(10, 3) == 3


class TestResolveRunnerStartRate:
    """测试 _resolve_runner_start_rate() 函数。"""

    def test_auto_returns_job_count(self):
        """auto 策略返回全部 job_count。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate("auto", 5) == 5

    def test_zero_job_count_returns_0(self):
        """job_count 为 0 时返回 0。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(3, 0) == 0

    def test_numeric_value(self):
        """数字值直接转换。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(2, 10) == 2

    def test_capped_at_job_count(self):
        """启动率不超过 job_count。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_start_rate

        assert _resolve_runner_start_rate(20, 5) == 5


class TestResolveRunnerTimeoutSeconds:
    """测试 _resolve_runner_timeout_seconds() 函数。"""

    def test_off_returns_zero(self):
        """off 禁用超时。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds("off") == 0.0

    def test_auto_returns_zero(self):
        """auto 默认不启用超时。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds("auto") == 0.0

    def test_numeric_seconds(self):
        """数字值转换为秒数。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds(30.5) == 30.5
        assert _resolve_runner_timeout_seconds("60") == 60.0

    def test_negative_returns_zero(self):
        """负数返回 0。"""
        from agent_py_agent.agent.agent_core.runner.dispatch import _resolve_runner_timeout_seconds

        assert _resolve_runner_timeout_seconds(-10) == 0.0


class TestRunnerTaskTimeout:
    """测试 runner 自动超时计算。"""

    def _timeout_config(self, runner_timeout_seconds: str = "auto"):
        """构造动态超时配置。"""

        config = MagicMock()
        config.runner_timeout_seconds = runner_timeout_seconds
        config.runner_timeout_by_role = {}
        config.dynamic_timeout_safety_margin = 2.0
        config.dynamic_timeout_min = 30
        config.dynamic_timeout_max = 600
        config.model_speed_profile_path = ""
        return config

    def test_off_runner_timeout_returns_no_limit(self):
        """用户配置 off/none/disabled 时，runner 不套超时墙。"""
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        task = MagicMock()
        task.attributes = {}
        task.goal = "实现示例网站 demo，包含注册、登录、流程状态和下单。"
        task.plan = ["write files", "verify behavior"]
        task.role = "worker"
        task.allowed_tools = ["read_file", "write_file"]

        assert get_task_timeout(task, 0.0, self._timeout_config("off")) == 0.0

    def test_off_runner_timeout_ignores_dynamic_timeout_attribute(self):
        """用户配置 off 时，失败后遗留的动态超时也不能重新启用超时。"""
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        task = MagicMock()
        task.attributes = {"dynamic_timeout_seconds": 300.0}
        task.goal = "恢复一个刚刚 timeout 的子代理任务。"
        task.plan = ["读取 continue packet", "继续执行"]
        task.role = "worker"
        task.allowed_tools = ["read_file", "write_file"]

        assert get_task_timeout(task, 0.0, self._timeout_config("off")) == 0.0

    def test_static_runner_timeout_still_overrides_no_limit_config(self):
        """用户显式数字超时时，仍按数字超时执行。"""
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        task = MagicMock()
        task.attributes = {}
        task.goal = "实现一个小改动。"
        task.plan = []
        task.role = "worker"
        task.allowed_tools = ["write_file"]

        assert get_task_timeout(task, 45.0, self._timeout_config("off")) == 45.0

    def test_role_timeout_override_can_disable_root_and_bound_worker(self):
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        config = self._timeout_config("8")
        config.runner_timeout_by_role = {"root": "off", "worker": "8"}

        root_task = MagicMock()
        root_task.id = "root-run"
        root_task.root_id = "root-run"
        root_task.parent_id = ""
        root_task.role = "coordinator"
        root_task.attributes = {}
        root_task.goal = "协调真实 E2E 测试。"
        root_task.plan = []

        worker_task = MagicMock()
        worker_task.id = "worker-run"
        worker_task.root_id = "root-run"
        worker_task.parent_id = "root-run"
        worker_task.role = "worker"
        worker_task.attributes = {}
        worker_task.goal = "写一个示例网站页面。"
        worker_task.plan = []

        assert get_task_timeout(root_task, 8.0, config) == 0.0
        assert get_task_timeout(worker_task, 8.0, config) == 8.0

    def test_top_level_worker_uses_worker_timeout_not_root_timeout(self):
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        config = self._timeout_config("off")
        config.runner_timeout_by_role = {"root": "off", "worker": "180"}

        task = MagicMock()
        task.id = "top-worker"
        task.root_id = "top-worker"
        task.parent_id = ""
        task.role = "worker"
        task.attributes = {}
        task.goal = "做三个家具品牌首页。"
        task.plan = []

        assert get_task_timeout(task, 0.0, config) == 180.0

    def test_role_timeout_worker_alias_matches_worker(self):
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        config = self._timeout_config("off")
        config.runner_timeout_by_role = {"worker": "7"}

        task = MagicMock()
        task.id = "leaf-run"
        task.root_id = "root-run"
        task.parent_id = "root-run"
        task.role = "worker"
        task.attributes = {}
        task.goal = "写一个页面。"
        task.plan = []

        assert get_task_timeout(task, 0.0, config) == 7.0

    def test_role_timeout_takeover_bucket_overrides_worker_alias(self):
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        config = self._timeout_config("off")
        config.runner_timeout_by_role = {"worker": "1", "takeover": "30"}

        task = MagicMock()
        task.id = "takeover-run"
        task.root_id = "root-run"
        task.parent_id = "root-run"
        task.role = "worker"
        task.attributes = {"takeover_source_run_id": "old-run"}
        task.goal = "接管一个失败 worker。"
        task.plan = []

        assert get_task_timeout(task, 0.0, config) == 30.0

    def test_role_timeout_auto_uses_dynamic_timeout(self):
        from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

        config = self._timeout_config("8")
        config.runner_timeout_by_role = {"worker": "auto"}
        config.dynamic_timeout_min = 30
        config.dynamic_timeout_max = 60

        task = MagicMock()
        task.id = "worker-run"
        task.root_id = "root-run"
        task.parent_id = "root-run"
        task.role = "worker"
        task.attributes = {}
        task.goal = "写一个需要一点时间的任务。"
        task.plan = []

        assert get_task_timeout(task, 8.0, config) >= 30.0
