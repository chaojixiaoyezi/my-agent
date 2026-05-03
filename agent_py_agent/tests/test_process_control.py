"""进程控制测试 - process_control.py 进程存活检测、终止信号、等待退出。"""
from __future__ import annotations

import os
import signal
import time
from unittest.mock import patch, MagicMock

import pytest


class TestIsPidAlive:
    """is_pid_alive 进程存活检测测试。"""

    def test_invalid_pid_zero(self):
        """验证 PID 为 0 返回 False。"""
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive
        assert is_pid_alive(0) is False

    def test_invalid_pid_negative(self):
        """验证负数 PID 返回 False。"""
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive
        assert is_pid_alive(-1) is False

    @patch("os.kill")
    def test_unix_process_alive(self, mock_kill):
        """验证 Unix 下进程存活返回 True。"""
        mock_kill.return_value = None
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive
        assert is_pid_alive(12345) is True
        mock_kill.assert_called_once_with(12345, 0)

    @patch("os.kill")
    def test_unix_process_dead(self, mock_kill):
        """验证 Unix 下进程不存在返回 False。"""
        mock_kill.side_effect = OSError("No such process")
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive
        assert is_pid_alive(99999) is False



class TestTerminatePid:
    """terminate_pid 进程终止信号测试。"""

    def test_invalid_pid_zero(self):
        """验证 PID 为 0 直接返回。"""
        from agent_py_agent.agent.gateway_parts.process_control import terminate_pid
        terminate_pid(0)

    def test_invalid_pid_negative(self):
        """验证负数 PID 直接返回。"""
        from agent_py_agent.agent.gateway_parts.process_control import terminate_pid
        terminate_pid(-1)

    @patch("os.kill")
    def test_terminate_success(self, mock_kill):
        """验证正常发送 SIGTERM。"""
        from agent_py_agent.agent.gateway_parts.process_control import terminate_pid
        terminate_pid(12345)
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @patch("os.kill")
    def test_terminate_already_dead(self, mock_kill):
        """验证进程已死时不抛异常。"""
        mock_kill.side_effect = OSError("No such process")
        from agent_py_agent.agent.gateway_parts.process_control import terminate_pid
        terminate_pid(99999)


class TestWaitForPidExit:
    """wait_for_pid_exit 进程退出等待测试。"""

    @patch("agent_py_agent.agent.gateway_parts.process_control.is_pid_alive")
    def test_exits_immediately(self, mock_is_alive):
        """验证进程立即退出返回 True。"""
        mock_is_alive.return_value = False
        from agent_py_agent.agent.gateway_parts.process_control import wait_for_pid_exit
        result = wait_for_pid_exit(12345, timeout=5.0)
        assert result is True

    @patch("agent_py_agent.agent.gateway_parts.process_control.is_pid_alive")
    @patch("time.sleep")
    @patch("time.time")
    def test_timeout_expires(self, mock_time, mock_sleep, mock_is_alive):
        """验证超时后返回 False。"""
        mock_times = [100.0, 100.2, 100.4, 100.6, 100.8]
        mock_time.side_effect = iter(mock_times)
        mock_is_alive.return_value = True
        from agent_py_agent.agent.gateway_parts.process_control import wait_for_pid_exit
        result = wait_for_pid_exit(12345, timeout=0.5)
        assert result is False

    @patch("agent_py_agent.agent.gateway_parts.process_control.is_pid_alive")
    @patch("time.sleep")
    @patch("time.time")
    def test_exits_before_deadline(self, mock_time, mock_sleep, mock_is_alive):
        """验证期限内退出返回 True。"""
        mock_times = [100.0, 100.2, 100.4, 100.6]
        mock_time.side_effect = iter(mock_times)
        mock_is_alive.side_effect = [True, True, False, False]
        from agent_py_agent.agent.gateway_parts.process_control import wait_for_pid_exit
        result = wait_for_pid_exit(12345, timeout=2.0)
        assert result is True

    def test_negative_timeout_handled(self):
        """验证负数超时被处理为 0。"""
        from agent_py_agent.agent.gateway_parts.process_control import wait_for_pid_exit
        with patch("agent_py_agent.agent.gateway_parts.process_control.is_pid_alive", return_value=False):
            result = wait_for_pid_exit(12345, timeout=-10.0)
            assert result is True
