"""runner_dispatch.py 单元测试。

测试 runner 任务分配、并发控制、超时处理等核心功能。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
        """off 策略返回 0 次补跑（不自动重试）。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("off") == 0
        assert _runner_max_attempts("none") == 0
        assert _runner_max_attempts("disabled") == 0

    def test_numeric_policy_returns_value(self):
        """数字策略返回对应失败后重试次数。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("3") == 3
        assert _runner_max_attempts(3) == 3
        assert _runner_max_attempts(1) == 1
        assert _runner_max_attempts("0") == 0
        assert _runner_max_attempts(0) == 0

    def test_invalid_policy_returns_2(self):
        """无效策略默认返回 2。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _runner_max_attempts

        assert _runner_max_attempts("invalid") == 2
        assert _runner_max_attempts("abc") == 2

    # LLM: zero runner attempts means no retry ceiling, not retry disabled.
    # 函数用途: 验证 runner_failure_policy=0 时可重试失败不会因 attempt 计数被提前卡死。
    def test_zero_policy_keeps_retry_candidate_unlimited(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=99,
        )

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=0, same_run_redispatch_limit=0) is True

    # LLM: runner retry limits count retries after the first failed attempt, not total attempts.
    # 函数用途: 验证 runner_failure_retry_limit=2 允许第一次失败后的两次补跑机会。
    def test_runner_failure_retry_limit_counts_retries_after_initial_attempt(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=2,
        )

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=2, same_run_redispatch_limit=0) is True
        task.runner_attempts = 3
        assert _is_dispatch_runner_candidate(task, runner_max_attempts=2, same_run_redispatch_limit=0) is False

    # LLM: same-run redispatch has its own cap so a parent can choose a different recovery strategy.
    # 函数用途: 验证 same_run_redispatch_limit=1 时，同一个 run_id 只允许失败后再派一次。
    def test_same_run_redispatch_limit_blocks_repeating_same_run(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

        task = SimpleNamespace(
            status="FAILED",
            verification_status="UNVERIFIED",
            channel_status="OK",
            capability_requests=[],
            capability_gaps=[],
            failure_type="runner_error",
            runner_attempts=2,
        )

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=3, same_run_redispatch_limit=1) is False
        assert _is_dispatch_runner_candidate(task, runner_max_attempts=3, same_run_redispatch_limit=0) is True


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


class TestRunnerCandidateCapabilityGrant:
    """测试能力授权后的 blocked runner 能继续执行。"""

    # LLM: granted permission blockers should re-enter runner selection without relying on retry attempts.
    # 函数用途: 子代理因为写权限阻塞后，父级授权完成时必须能被 dispatch 再跑一轮。
    def test_permission_blocked_with_grant_is_runner_candidate(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

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

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=1) is True

    # LLM: open requests remain a hard stop even if an older grant exists.
    # 函数用途: 仍有 OPEN capability_request 时不能提前重跑，避免模型在未授权状态反复失败。
    def test_open_request_still_blocks_runner_candidate(self):
        from agent_py_agent.agent.agent_core.runner_dispatch import _is_dispatch_runner_candidate

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

        assert _is_dispatch_runner_candidate(task, runner_max_attempts=2) is False


class TestResolveRunnerConcurrency:
    """测试 _resolve_runner_concurrency() 函数。"""

    def test_auto_uses_bounded_job_count(self):
        """auto 策略按任务数并发，但受内部安全上限约束。"""
        from agent_py_agent.agent.agent_core.runner_dispatch import _resolve_runner_concurrency

        assert _resolve_runner_concurrency("auto", 5) == 5
        assert _resolve_runner_concurrency("", 5) == 5
        assert _resolve_runner_concurrency("auto", 20) == 8

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
        config.runner_timeout_by_role = {}
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
        task.goal = "实现示例网站 demo，包含注册、登录、流程状态和下单。"
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

    # LLM: role timeout overrides let root stay alive while leaf workers are intentionally bounded.
    # 函数用途: 验证 root/coordinator/worker 可以使用不同 runner timeout，不再为了测试 worker 超时误杀 root。
    def test_role_timeout_override_can_disable_root_and_bound_worker(self):
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

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

    # LLM: top-level worker runs are still workers, not unlimited root coordinators.
    # 函数用途: 复现真实 E2E 中顶层 worker 因 parent_id 为空误吃 root=off，导致 worker 时间上限失效的问题。
    def test_top_level_worker_uses_worker_timeout_not_root_timeout(self):
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

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

    # LLM: user-facing worker timeout should cover internal concrete worker roles.
    # 函数用途: 验证 leaf_worker 这类内部角色名会自动匹配用户配置的 worker 超时。
    def test_role_timeout_worker_alias_matches_leaf_worker(self):
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

        config = self._timeout_config("off")
        config.runner_timeout_by_role = {"worker": "7"}

        task = MagicMock()
        task.id = "leaf-run"
        task.root_id = "root-run"
        task.parent_id = "root-run"
        task.role = "leaf_worker"
        task.attributes = {}
        task.goal = "写一个页面。"
        task.plan = []

        assert get_task_timeout(task, 0.0, config) == 7.0

    # LLM: takeover timeout bucket lets recovery runs get a different budget than the failed worker.
    # 函数用途: 验证接管 run 优先匹配 takeover 超时桶，再回退到 leaf_worker/worker。
    def test_role_timeout_takeover_bucket_overrides_worker_alias(self):
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

        config = self._timeout_config("off")
        config.runner_timeout_by_role = {"worker": "1", "takeover": "30"}

        task = MagicMock()
        task.id = "takeover-run"
        task.root_id = "root-run"
        task.parent_id = "root-run"
        task.role = "leaf_worker"
        task.attributes = {"takeover_source_run_id": "old-run"}
        task.goal = "接管一个失败 worker。"
        task.plan = []

        assert get_task_timeout(task, 0.0, config) == 30.0

    # LLM: role auto should mean dynamic timeout even when the global config is fixed.
    # 函数用途: 验证角色级 auto 可以绕过全局固定超时，使用动态估算。
    def test_role_timeout_auto_uses_dynamic_timeout(self):
        from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

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
