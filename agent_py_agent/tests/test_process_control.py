"""进程控制测试 - process_control.py 进程存活检测、终止信号、等待退出。"""
from __future__ import annotations

import signal
import time
from unittest.mock import MagicMock, patch

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

    def test_posix_process_alive(self, monkeypatch):
        """验证 Unix 下进程存活返回 True。"""
        from agent_py_agent.agent.gateway_parts import process_control
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive

        mock_kill = MagicMock(return_value=None)
        monkeypatch.setattr(process_control.os, "name", "posix")
        monkeypatch.setattr(process_control.os, "kill", mock_kill)

        assert is_pid_alive(12345) is True
        mock_kill.assert_called_once_with(12345, 0)

    def test_posix_process_dead(self, monkeypatch):
        """验证 Unix 下进程不存在返回 False。"""
        from agent_py_agent.agent.gateway_parts import process_control
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive

        mock_kill = MagicMock(side_effect=OSError("No such process"))
        monkeypatch.setattr(process_control.os, "name", "posix")
        monkeypatch.setattr(process_control.os, "kill", mock_kill)

        assert is_pid_alive(99999) is False

    def test_windows_process_alive(self, monkeypatch):
        """验证 Windows 下通过 OpenProcess/GetExitCodeProcess 检查存活。"""
        from agent_py_agent.agent.gateway_parts import process_control
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive

        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 123
        kernel32.GetExitCodeProcess.side_effect = lambda _handle, exit_code: setattr(exit_code._obj, "value", 259) or True
        windll = MagicMock(kernel32=kernel32)

        monkeypatch.setattr(process_control.os, "name", "nt")
        monkeypatch.setattr(process_control.ctypes, "windll", windll, raising=False)

        assert is_pid_alive(12345) is True
        kernel32.OpenProcess.assert_called_once()
        kernel32.CloseHandle.assert_called_once_with(123)

    def test_windows_process_missing(self, monkeypatch):
        """验证 Windows 下 OpenProcess 失败时返回 False。"""
        from agent_py_agent.agent.gateway_parts import process_control
        from agent_py_agent.agent.gateway_parts.process_control import is_pid_alive

        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 0
        windll = MagicMock(kernel32=kernel32)

        monkeypatch.setattr(process_control.os, "name", "nt")
        monkeypatch.setattr(process_control.ctypes, "windll", windll, raising=False)

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
