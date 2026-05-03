"""gateway_commands CLI 命令测试。

测试 gateway start/stop/status/restart/logs/supervisor 命令。
注意：supervisor 命令在 supervisor.py 中，start-all 在 supervisor.py 中。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


class TestCmdGatewayStart:
    """测试 cmd_gateway_start 命令。"""

    def test_gateway_start_already_running(self, tmp_path: Path):
        """Gateway 已运行时直接返回成功。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_start

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.force = False
        args.force_lock = False

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.state = tmp_path / "state.json"
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=12345), \
             patch("agent_py_agent.cli.gateway_process.is_pid_alive", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value={"start_time": "2024-01-01"}):
            result = cmd_gateway_start(args)
            assert result == 0

    def test_gateway_start_force_restart(self, tmp_path: Path):
        """带 --force 参数时停止旧进程然后启动新的。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_start

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.force = True
        args.force_lock = False

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.state = tmp_path / "state.json"
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=12345), \
             patch("agent_py_agent.cli.gateway_process.is_pid_alive", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.wait_for_pid_exit", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.terminate_pid"), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths") as mock_gw_paths:
            mock_gw_paths.return_value = mock_paths
            result = cmd_gateway_start(args)
            # force 模式会尝试停止旧进程然后启动新的
            assert result in (0, 1)


class TestCmdGatewayStop:
    """测试 cmd_gateway_stop 命令。"""

    def test_gateway_stop_not_running(self, tmp_path: Path):
        """Gateway 未运行时直接返回成功。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_stop

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 10.0
        args.kill = False
        args.reason = "用户请求停止"

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.remove_pid_file_if_owned"):
            result = cmd_gateway_stop(args)
            assert result == 0

    def test_gateway_stop_already_stopped(self, tmp_path: Path):
        """Gateway 未运行时打印提示并返回。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_stop

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 1.0
        args.kill = False
        args.reason = "测试"

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.remove_pid_file_if_owned"):
            result = cmd_gateway_stop(args)
            assert result == 0


class TestCmdGatewayStatus:
    """测试 cmd_gateway_status 命令。"""

    def test_gateway_status_no_process(self, tmp_path: Path):
        """Gateway 无进程时显示状态。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stale_seconds = 300

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.state = tmp_path / "state.json"
        mock_paths.heartbeat = tmp_path / "heartbeat.json"
        mock_paths.log = tmp_path / "gateway.log"

        # 准备空的 state 和 heartbeat 文件
        mock_paths.state.write_text("{}", encoding="utf-8")
        mock_paths.heartbeat.write_text("{}", encoding="utf-8")

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.read_json_file", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.read_runtime_status", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}):
            result = cmd_gateway_status(args)
            assert result == 0


class TestCmdGatewayRestart:
    """测试 cmd_gateway_restart 命令。"""

    def test_gateway_restart_with_stopped_gateway(self, tmp_path: Path):
        """重启已停止的 gateway。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_restart

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 10.0
        args.force = False
        args.force_lock = False

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.state = tmp_path / "state.json"
        mock_paths.stop_request = tmp_path / "stop.json"
        mock_paths.heartbeat = tmp_path / "heartbeat.json"
        mock_paths.log = tmp_path / "gateway.log"

        mock_paths.state.write_text("{}", encoding="utf-8")
        mock_paths.heartbeat.write_text("{}", encoding="utf-8")

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.read_json_file", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.read_runtime_status", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.wait_for_gateway_running", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.cmd_gateway_start", return_value=0):
            result = cmd_gateway_restart(args)
            assert result == 0


class TestCmdGatewayLogs:
    """测试 cmd_gateway_logs 命令。"""

    def test_gateway_logs_no_file(self, tmp_path: Path):
        """日志文件不存在时返回成功。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_logs

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.lines = 80

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.log = tmp_path / "gateway.log"

        # 不创建日志文件

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths):
            result = cmd_gateway_logs(args)
            assert result == 0

    def test_gateway_logs_with_content(self, tmp_path: Path):
        """日志文件有时显示内容。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_logs

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.lines = 10

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.log = tmp_path / "gateway.log"

        # 创建日志目录和文件
        mock_paths.log.parent.mkdir(parents=True, exist_ok=True)
        mock_paths.log.write_text("line1\nline2\nline3\n", encoding="utf-8")

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths):
            result = cmd_gateway_logs(args)
            assert result == 0


class TestCmdSupervisorStart:
    """测试 cmd_supervisor_start 命令（来自 supervisor.py）。"""

    def test_supervisor_start_already_running(self, tmp_path: Path):
        """Supervisor 已运行时直接返回。"""
        from agent_py_agent.cli.supervisor import cmd_supervisor_start

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.log = tmp_path / "supervisor.log"

        with patch("agent_py_agent.cli.supervisor.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.supervisor.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.supervisor.is_supervisor_running", return_value=True):
            result = cmd_supervisor_start(args)
            assert result == 0


class TestCmdSupervisorStatus:
    """测试 cmd_supervisor_status 命令（来自 supervisor.py）。"""

    def test_supervisor_status_not_running(self, tmp_path: Path):
        """Supervisor 未运行时显示提示。"""
        from agent_py_agent.cli.supervisor import cmd_supervisor_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.state = tmp_path / "state.json"
        mock_paths.heartbeat = tmp_path / "heartbeat.json"

        # 不创建 supervisor.pid 文件

        with patch("agent_py_agent.cli.supervisor.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.supervisor.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.supervisor.gateway_running", return_value=(None, False)):
            result = cmd_supervisor_status(args)
            assert result == 0


class TestCmdSupervisorStop:
    """测试 cmd_supervisor_stop 命令（来自 supervisor.py）。"""

    def test_supervisor_stop_success(self, tmp_path: Path):
        """成功停止 supervisor。"""
        from agent_py_agent.cli.supervisor import cmd_supervisor_stop

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 10.0

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path

        with patch("agent_py_agent.cli.supervisor.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.supervisor.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.supervisor.stop_supervisor", return_value=True):
            result = cmd_supervisor_stop(args)
            assert result == 0

    def test_supervisor_stop_timeout(self, tmp_path: Path):
        """Supervisor 停止超时。"""
        from agent_py_agent.cli.supervisor import cmd_supervisor_stop

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 1.0

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path

        with patch("agent_py_agent.cli.supervisor.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.supervisor.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.supervisor.stop_supervisor", return_value=False):
            result = cmd_supervisor_stop(args)
            assert result == 2


class TestCmdStartAll:
    """测试 cmd_start_all 命令（来自 supervisor.py）。"""

    def test_start_all_supervisor_already_running(self, tmp_path: Path):
        """Supervisor 已运行时跳过启动步骤。"""
        from agent_py_agent.cli.supervisor import cmd_start_all

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.adapter_channel = "none"  # 使用 none 避免 adapter 启动

        mock_agent = MagicMock()

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.log = tmp_path / "start-all.log"

        with patch("agent_py_agent.cli.supervisor.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.supervisor.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.supervisor.is_supervisor_running", return_value=True), \
             patch("agent_py_agent.cli.supervisor.gateway_running", return_value=(12345, True)):
            result = cmd_start_all(args)
            assert result == 0


class TestCmdGatewayInstall:
    """测试 cmd_gateway_install 命令。"""

    def test_gateway_install_success(self, tmp_path: Path):
        """成功安装 gateway 服务。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_install

        args = MagicMock()
        args.force = False

        with patch("agent_py_agent.cli.gateway_process.install_service", return_value=True):
            result = cmd_gateway_install(args)
            assert result == 0


class TestCmdGatewayUninstall:
    """测试 cmd_gateway_uninstall 命令。"""

    def test_gateway_uninstall_success(self, tmp_path: Path):
        """成功卸载 gateway 服务。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_uninstall

        args = MagicMock()

        with patch("agent_py_agent.cli.gateway_process.uninstall_service", return_value=True):
            result = cmd_gateway_uninstall(args)
            assert result == 0
