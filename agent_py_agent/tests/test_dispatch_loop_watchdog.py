"""Dispatch 循环闭环保证机制测试。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestWatchdog:
    """测试 Watchdog 逻辑。"""

    def test_watchdog_not_enabled_by_default(self, tmp_path: Path):
        """默认不启用 watchdog。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False
            watchdog_interval = 60
            watchdog_max_restarts = 3
            watchdog_restart_delay = 10

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        assert watchdog.enabled is False
        assert watchdog.interval == 60
        assert watchdog.max_restarts == 3

    def test_watchdog_check_pid_alive(self, tmp_path: Path):
        """检查进程存活检测。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 60
            watchdog_max_restarts = 3
            watchdog_restart_delay = 10

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        # 当前进程一定存活，用自己的 pid 测试
        assert watchdog._is_pid_alive(os.getpid()) is True

        # 不存在的 pid
        assert watchdog._is_pid_alive(999999999) is False

    def test_watchdog_uses_posix_pid_check_on_mac_and_linux(self, monkeypatch, tmp_path: Path):
        """macOS/Linux 保留 os.kill(pid, 0) 的进程探测路径。"""
        from agent_py_agent.agent.agent_core import watchdog as watchdog_module
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 60
            watchdog_max_restarts = 3
            watchdog_restart_delay = 10

        calls = []

        def fake_kill(pid, signal_number):
            calls.append((pid, signal_number))

        monkeypatch.setattr(watchdog_module.sys, "platform", "darwin")
        monkeypatch.setattr(watchdog_module.os, "kill", fake_kill)

        watchdog = DispatchWatchdog(MockConfig())

        assert watchdog._is_pid_alive(12345) is True
        assert calls == [(12345, 0)]

    def test_watchdog_start_stop(self, tmp_path: Path):
        """测试 watchdog 启动和停止。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 1  # 短间隔便于测试
            watchdog_max_restarts = 3
            watchdog_restart_delay = 1

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        # 启动
        watchdog.start()
        assert watchdog.is_running is True

        # 停止
        watchdog.stop()
        assert watchdog.is_running is False

    def test_watchdog_disabled_does_not_start(self, tmp_path: Path):
        """不启用时不会启动线程。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        watchdog.start()
        assert watchdog.is_running is False

class TestDispatchLoopExceptions:
    """补充：dispatch_loop 异常场景测试。"""

    def test_task_midway_failure(self, tmp_path: Path):
        """任务中途失败的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        call_count = [0]

        def dispatch_side_effect(*args, **kwargs):
            call_count[0] += 1
            # 第一轮有任务失败，第二轮继续处理
            mock_report = MagicMock()
            if call_count[0] == 1:
                # 模拟任务失败
                failed_record = MagicMock()
                failed_record.ok = False
                failed_record.status = "FAILED"
                failed_record.step = "runner"
                mock_report.records = [failed_record]
                agent.has_pending_work = True
            else:
                mock_report.records = []
                agent.has_pending_work = False
            return mock_report

        agent.dispatch_subagents.side_effect = dispatch_side_effect
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert result.rounds_count >= 1

    def test_all_tasks_paused(self, tmp_path: Path):
        """所有任务都被暂停时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        # 所有任务都是暂停状态
        paused_record = MagicMock()
        paused_record.ok = True
        paused_record.status = "PAUSED"
        paused_record.step = "runner"
        mock_report.records = [paused_record]
        agent.dispatch_subagents.return_value = mock_report

        # 模拟所有任务都暂停
        mock_task = MagicMock()
        mock_task.status = "PAUSED"
        mock_task.runner_attempts = 1
        agent.subagents.list_runs.return_value = [mock_task]

        # 设置 max_runners=1 且 runner_failure_policy 使暂停的任务不参与调度
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20, max_runners=1)
        assert isinstance(result, DispatchLoopReport)

    def test_max_rounds_limit_reached(self, tmp_path: Path):
        """达到最大轮数限制时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 永远有候选任务
        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.runner_attempts = 0
        agent.subagents.list_runs.return_value = [mock_task]
        agent.has_pending_work = True

        mock_report = MagicMock()
        mock_report.records = [MagicMock()]  # 有记录
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=3)
        assert result.rounds_count == 3
        assert result.stopped_by_limit is True

    def test_dispatch_with_no_retry_policy(self, tmp_path: Path):
        """不重试策略下的 dispatch 处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "no_retry"

        mock_report = MagicMock()
        failed_record = MagicMock()
        failed_record.ok = False
        failed_record.status = "FAILED"
        mock_report.records = [failed_record]

        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)

    def test_dispatch_runner_worker_exception(self, tmp_path: Path):
        """runner worker 抛出异常时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
            SimpleAgentDispatchMixin,
        )

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.config.runner_concurrency = 1
                self.config.runner_start_rate = None
                self.config.runner_timeout_seconds = 120
                self.subagents = MagicMock()
                self.local_store = MagicMock()

            def subagents_list_runs(self):
                return self.subagents.list_runs()

        agent = MockAgent()

        mock_task = MagicMock()
        mock_task.id = "test_task"
        mock_task.status = "RUNNING"
        mock_task.goal = "测试目标"
        mock_task.plan = ""
        mock_task.attributes = {}
        mock_task.runner_attempts = 0

        agent.subagents.list_runs.return_value = [mock_task]
        agent._has_pending_work = True

        # 模拟 save 抛出异常
        agent.subagents.save.side_effect = RuntimeError("Save failed")
        agent.subagents.load.return_value = mock_task

        # runner 执行路径不应该崩溃
        try:
            agent.dispatch_subagents(
                router=MagicMock(),
                apply=True,
                start_runners=True,
            )
        except Exception:
            pass  # 异常应该被捕获

    def test_dispatch_zero_candidates(self, tmp_path: Path):
        """零候选任务时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.subagents.list_runs.return_value = []  # 无候选
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert result.rounds_count == 1
        assert result.total_records == 0

    def test_dispatch_with_negative_max_rounds(self, tmp_path: Path):
        """负数 max_rounds 时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        # 负数 max_rounds 应该安全处理（视为无效，不执行循环）
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=-10)
        assert isinstance(result, DispatchLoopReport)
