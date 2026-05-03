"""看门狗测试 - watchdog.py 进程监控、自动重启。"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDispatchWatchdog:
    """测试 DispatchWatchdog 类。"""

    def test_watchdog_disabled_by_default(self, tmp_path: Path):
        """默认不启用。"""
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

    def test_watchdog_config_loading(self, tmp_path: Path):
        """配置正确加载。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 30
            watchdog_max_restarts = 5
            watchdog_restart_delay = 15

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        assert watchdog.enabled is True
        assert watchdog.interval == 30
        assert watchdog.max_restarts == 5
        assert watchdog.restart_delay == 15

    def test_watchdog_pid_file_path(self, tmp_path: Path):
        """PID 文件路径正确。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        expected = Path(tmp_path) / "gateway" / "gateway.pid"
        assert watchdog.pid_file == expected

    def test_is_pid_alive_current_process(self, tmp_path: Path):
        """当前进程被认为是存活的。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        assert watchdog._is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_nonexistent(self, tmp_path: Path):
        """不存在的 PID 被认为不存活。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        assert watchdog._is_pid_alive(999999999) is False

    def test_watchdog_start_without_thread_when_disabled(self, tmp_path: Path):
        """禁用时不启动线程。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        watchdog.start()
        assert watchdog.is_running is False

    def test_watchdog_stop_with_timeout(self, tmp_path: Path):
        """停止时等待线程结束。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 1
            watchdog_max_restarts = 3
            watchdog_restart_delay = 1

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        watchdog.start()
        assert watchdog._thread is not None

        watchdog.stop(timeout=2.0)
        assert watchdog._thread is None

    def test_watchdog_is_running_state(self, tmp_path: Path):
        """is_running 属性正确反映状态。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = True
            watchdog_interval = 1
            watchdog_max_restarts = 3
            watchdog_restart_delay = 1

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        assert watchdog.is_running is False

        watchdog.start()
        time.sleep(0.1)
        assert watchdog.is_running is True

        watchdog.stop()
        assert watchdog.is_running is False


class TestWatchdogCheckDaemon:
    """测试 daemon 检查逻辑。"""

    def test_check_no_pid_file(self, tmp_path: Path):
        """无 PID 文件时的处理。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        # 不存在 PID 文件
        with patch.object(watchdog, "_log") as mock_log:
            watchdog._check_daemon()
            mock_log.assert_called_once_with("PID file not found, daemon may not be running")

    def test_check_invalid_pid_content(self, tmp_path: Path):
        """PID 文件内容无效时的处理。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        pid_file = Path(tmp_path) / "gateway" / "gateway.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("not a number", encoding="utf-8")

        with patch.object(watchdog, "_log") as mock_log:
            watchdog._check_daemon()
            mock_log.assert_called()

    def test_check_daemon_alive(self, tmp_path: Path):
        """daemon 存活时的处理。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        pid_file = Path(tmp_path) / "gateway" / "gateway.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with patch.object(watchdog, "_log") as mock_log:
            watchdog._check_daemon()
            mock_log.assert_called_with(f"Daemon is alive (pid={os.getpid()})")

    def test_check_daemon_dead(self, tmp_path: Path):
        """daemon 死亡时的处理。"""
        from agent_py_agent.agent.agent_core.watchdog import DispatchWatchdog

        class MockConfig:
            workspace_root = str(tmp_path)
            gateway_workspace = "gateway"
            watchdog_enabled = False

        config = MockConfig()
        watchdog = DispatchWatchdog(config)

        pid_file = Path(tmp_path) / "gateway" / "gateway.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("999999999", encoding="utf-8")

        with patch.object(watchdog, "_log") as mock_log:
            with patch.object(watchdog, "_handle_dead_daemon") as mock_handle:
                watchdog._check_daemon()
                mock_handle.assert_called_once_with(999999999)