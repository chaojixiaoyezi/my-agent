"""gateway_commands CLI 命令测试。

测试 gateway start/stop/status/restart/logs/supervisor 命令。
注意：supervisor 命令在 supervisor.py 中，start-all 在 supervisor.py 中。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.state = tmp_path / "state.json"
        mock_paths.heartbeat = tmp_path / "heartbeat.json"
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"
        mock_paths.log = tmp_path / "gateway.log"

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

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.state = tmp_path / "state.json"
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.stop_request = tmp_path / "stop.json"
        mock_paths.log = tmp_path / "gateway.log"

        mock_process = MagicMock()
        mock_process.pid = 99999

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=12345), \
             patch("agent_py_agent.cli.gateway_process.is_pid_alive", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.wait_for_pid_exit", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.terminate_pid"), \
             patch("agent_py_agent.cli.gateway_process._wait_for_gateway_start_ready", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.write_json_file"), \
             patch("subprocess.Popen", return_value=mock_process), \
             patch("agent_py_agent.agent.gateway_parts.daemon_control._get_process_start_time", return_value="2026-01-01T00:00:00"), \
             patch("agent_py_agent.agent.gateway_parts.daemon_control._utc_now_iso", return_value="2026-01-01T00:00:00"):
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

    def test_gateway_stop_targets_exact_running_process(self, tmp_path: Path):
        """运行中 Gateway 的停止文件必须携带精确 PID 身份。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_stop

        args = MagicMock(
            config=str(tmp_path / "config.yaml"),
            timeout=1.0,
            kill=False,
            reason="测试精确停止",
        )
        mock_agent = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5
        paths = SimpleNamespace(
            root=tmp_path,
            pid=tmp_path / "gateway.pid",
            stop_request=tmp_path / "gateway.stop",
        )

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=paths), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=43210), \
             patch("agent_py_agent.cli.gateway_process.wait_for_pid_exit", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.log_gateway_event"):
            assert cmd_gateway_stop(args) == 0

        payload = json.loads(paths.stop_request.read_text(encoding="utf-8"))
        assert payload["reason"] == "测试精确停止"
        assert payload["target_process"]["pid"] == 43210
        assert payload["source"] == "gateway_cli"


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
        type(mock_agent.config).gateway_port = PropertyMock(return_value=0)

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.state = tmp_path / "gateway_state.json"
        mock_paths.heartbeat = tmp_path / "gateway_heartbeat.json"
        mock_paths.log = tmp_path / "gateway.log"

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}):
            result = cmd_gateway_status(args)
            assert result == 0

    def test_gateway_status_reports_bad_state_json(self, tmp_path: Path, capsys):
        from agent_py_agent.cli.gateway_process import cmd_gateway_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        state_path = tmp_path / "gateway_state.json"
        state_path.write_text("{bad state", encoding="utf-8")

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        type(mock_agent.config).gateway_port = PropertyMock(return_value=0)
        mock_paths = MagicMock(
            root=tmp_path,
            pid=tmp_path / "gateway.pid",
            state=state_path,
            heartbeat=tmp_path / "gateway_heartbeat.json",
        )

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}):
            assert cmd_gateway_status(args) == 0

        assert "state_load_error=" in capsys.readouterr().out

    def test_gateway_status_marks_running_state_without_pid_as_stale(self, tmp_path: Path, capsys):
        from agent_py_agent.cli.gateway_process import cmd_gateway_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        state_path = tmp_path / "gateway_state.json"
        state_path.write_text('{"status": "running", "pid": 123}', encoding="utf-8")

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        type(mock_agent.config).gateway_port = PropertyMock(return_value=0)
        mock_paths = MagicMock(
            root=tmp_path,
            pid=tmp_path / "gateway.pid",
            state=state_path,
            heartbeat=tmp_path / "gateway_heartbeat.json",
        )

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}):
            assert cmd_gateway_status(args) == 0

        out = capsys.readouterr().out
        assert "gateway status=stopped pid=none state=stale-running" in out
        assert '"stale_state": true' in out

    def test_gateway_status_counts_only_request_json_files(self, tmp_path: Path, capsys):
        from agent_py_agent.cli.gateway_process import cmd_gateway_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        state_path = tmp_path / "gateway_state.json"
        state_path.write_text('{"status": "running"}', encoding="utf-8")
        inbox = tmp_path / "requests" / "pending"
        processing = tmp_path / "requests" / "processing"
        inbox.mkdir(parents=True)
        processing.mkdir(parents=True)
        (inbox / "gw-1.json").write_text("{}", encoding="utf-8")
        (inbox / "gw-1.chunks.jsonl").write_text("{}", encoding="utf-8")
        (processing / "gw-2.json").write_text("{}", encoding="utf-8")
        (processing / "gw-2.json.lock").write_text("", encoding="utf-8")

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        type(mock_agent.config).gateway_port = PropertyMock(return_value=0)
        mock_paths = MagicMock(
            root=tmp_path,
            pid=tmp_path / "gateway.pid",
            state=state_path,
            heartbeat=tmp_path / "gateway_heartbeat.json",
        )
        mock_paths.inbox = inbox
        mock_paths.processing = processing

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value={"pid": 123}), \
             patch("agent_py_agent.cli.gateway_process.is_pid_alive", return_value=True):
            assert cmd_gateway_status(args) == 0

        out = capsys.readouterr().out
        assert "inbox=1 processing=1" in out


class TestCmdGatewayRestart:
    """测试 cmd_gateway_restart 命令。"""

    def test_gateway_restart_starts_background_gateway(self):
        """Restart should stop first, then return after starting the background gateway."""
        from agent_py_agent.cli.gateway_process import cmd_gateway_restart

        args = MagicMock(
            config="agent_config.yaml",
            timeout=None,
            force=False,
        )

        with patch("agent_py_agent.cli.gateway_process.cmd_gateway_stop", return_value=0) as mock_stop, \
             patch("agent_py_agent.cli.gateway_process.cmd_gateway_start", return_value=0) as mock_start:
            result = cmd_gateway_restart(args)

        assert result == 0
        assert mock_stop.call_args.args[0].kill is True
        start_args = mock_start.call_args.args[0]
        assert start_args.config == "agent_config.yaml"
        assert start_args.force is True

    def test_gateway_restart_with_stopped_gateway(self, tmp_path: Path):
        """重启已停止的 gateway。"""
        from agent_py_agent.cli.gateway_process import cmd_gateway_restart

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.timeout = 10.0
        args.force = False

        mock_agent = MagicMock()
        mock_agent.config = MagicMock()
        mock_agent.config.gateway_stop_timeout = 5
        mock_agent.config.gateway_port = 0

        mock_paths = MagicMock()
        mock_paths.root = tmp_path
        mock_paths.pid = tmp_path / "gateway.pid"
        mock_paths.state = MagicMock()
        mock_paths.stop_request = tmp_path / "stop.json"
        mock_paths.heartbeat = MagicMock()
        mock_paths.log = tmp_path / "gateway.log"
        mock_paths.state.exists = MagicMock(return_value=False)
        mock_paths.heartbeat.exists = MagicMock(return_value=False)

        with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=mock_paths), \
             patch("agent_py_agent.cli.gateway_process.read_pid_record", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.read_json_file", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.gateway_process.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=None), \
             patch("agent_py_agent.cli.gateway_process.wait_for_gateway_running", return_value=True), \
             patch("agent_py_agent.cli.gateway_process.cmd_gateway_start", return_value=0) as mock_start:
            result = cmd_gateway_restart(args)
            assert result == 0
            mock_start.assert_called_once()
            result = cmd_gateway_restart(args)
            assert result == 0


class TestGatewayRunStateHelpers:
    """Gateway run state helper regressions."""

    def test_record_gateway_run_failed_persists_failed_state(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_process import _record_gateway_run_failed

        paths = MagicMock()
        paths.state = tmp_path / "gateway_state.json"
        agent = MagicMock()

        with patch("agent_py_agent.cli.gateway_process.log_gateway_event"):
            _record_gateway_run_failed(paths, agent, 123, RuntimeError("boom"))

        payload = json.loads(paths.state.read_text(encoding="utf-8"))
        assert payload["status"] == "failed"
        assert payload["pid"] == 123
        assert payload["error"] == "boom"

    def test_gateway_service_return_without_stop_is_failed(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_process import _classify_gateway_service_return
        from agent_py_agent.cli.models import GatewayRunContext

        context = GatewayRunContext(
            agent=SimpleNamespace(),
            paths=SimpleNamespace(stop_request=tmp_path / "gateway.stop"),
            config_path=tmp_path / "config.yaml",
        )

        termination = _classify_gateway_service_return(
            context,
            {"summary": "service returned"},
        )

        assert termination.status == "failed"
        assert termination.kind == "unexpected_service_loop_return"
        assert termination.exit_code == 2

    def test_gateway_service_stop_is_planned(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_process import _classify_gateway_service_return
        from agent_py_agent.cli.models import GatewayRunContext

        stop_path = tmp_path / "gateway.stop"
        stop_path.write_text('{"reason":"operator restart"}', encoding="utf-8")
        context = GatewayRunContext(
            agent=SimpleNamespace(),
            paths=SimpleNamespace(stop_request=stop_path),
            config_path=tmp_path / "config.yaml",
        )
        stopped = _classify_gateway_service_return(context, {"summary": "done"})

        assert (stopped.status, stopped.kind, stopped.reason, stopped.exit_code) == (
            "stopped",
            "planned_stop",
            "operator restart",
            0,
        )

    def test_gateway_signal_stop_is_typed_and_keeps_forensics(self, tmp_path: Path):
        import signal

        from agent_py_agent.cli.gateway_process import (
            _classify_gateway_service_return,
            _record_gateway_signal_stop_request,
        )
        from agent_py_agent.cli.models import GatewayRunContext

        stop_path = tmp_path / "gateway.stop"
        paths = SimpleNamespace(stop_request=stop_path)
        payload = _record_gateway_signal_stop_request(paths, signal.SIGTERM)
        context = GatewayRunContext(
            agent=SimpleNamespace(),
            paths=paths,
            config_path=tmp_path / "config.yaml",
        )

        termination = _classify_gateway_service_return(context, {"summary": "drained"})

        assert payload["source"] == "signal"
        assert payload["signal"]["name"] == "SIGTERM"
        assert payload["signal"]["pid"] > 0
        assert termination.kind == "signal_shutdown"
        assert termination.exit_code == 0
        assert termination.details["signal"]["name"] == "SIGTERM"

    def test_gateway_signal_does_not_relabel_preexisting_planned_stop(self, tmp_path: Path):
        import signal

        from agent_py_agent.cli.gateway_process import _record_gateway_signal_stop_request

        stop_path = tmp_path / "gateway.stop"
        stop_path.write_text('{"requested_at":1,"reason":"operator restart"}', encoding="utf-8")

        payload = _record_gateway_signal_stop_request(SimpleNamespace(stop_request=stop_path), signal.SIGTERM)

        assert payload["reason"] == "operator restart"
        assert "source" not in payload
        assert payload["signal_observed"]["preexisting_stop_request"] is True

    def test_gateway_start_command_uses_options_bundle(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_process import _gateway_start_command
        from agent_py_agent.cli.models import GatewayStartOptions

        config = tmp_path / "config.yaml"
        command = _gateway_start_command(GatewayStartOptions(config=config))

        assert command[-2:] == ["gateway", "run"]
        assert command[command.index("--config") + 1] == str(config.resolve())

    def test_gateway_start_command_passes_workspace_root(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_process import _gateway_start_command
        from agent_py_agent.cli.models import GatewayStartOptions

        config = tmp_path / "config.yaml"
        workspace = tmp_path / "all-agent"
        command = _gateway_start_command(
            GatewayStartOptions(config=config, workspace_root=str(workspace))
        )

        assert command[command.index("--workspace-root") + 1] == str(workspace.resolve())

    def test_gateway_worker_agent_reuses_initialized_context_agent(self, tmp_path: Path):
        from agent_py_agent.cli.gateway_loops import _gateway_agent_from_context
        from agent_py_agent.cli.models import GatewayRunContext

        initialized = SimpleNamespace(root=tmp_path / "all-agent")
        context = GatewayRunContext(
            agent=initialized,
            paths=SimpleNamespace(),
            config_path=tmp_path / "config.yaml",
        )

        with patch("agent_py_agent.cli.common.make_agent") as mock_make_agent:
            resolved = _gateway_agent_from_context(context)

        assert resolved is initialized
        mock_make_agent.assert_not_called()

    def test_gateway_service_loop_waits_only_for_stop_record(self, tmp_path: Path):
        import threading
        import time

        from agent_py_agent.cli.gateway_process import _run_gateway_service_loop
        from agent_py_agent.cli.models import GatewayRunContext

        stop_path = tmp_path / "gateway.stop"
        agent = MagicMock()
        context = GatewayRunContext(
            agent=agent,
            paths=SimpleNamespace(stop_request=stop_path),
            config_path=tmp_path / "config.yaml",
        )
        timer = threading.Timer(0.02, lambda: stop_path.write_text("{}", encoding="utf-8"))
        timer.start()
        started = time.monotonic()
        try:
            report = _run_gateway_service_loop(context)
        finally:
            timer.cancel()

        assert time.monotonic() - started >= 0.02
        assert report == {"summary": "stop requested", "stop_request": {}}
        agent.watch_subagents.assert_not_called()

    def test_gateway_service_loop_ignores_previous_process_stop(self, tmp_path: Path):
        """上一代 PID 的停止文件不会结束新 Gateway，直到本代请求到达。"""
        import threading

        from agent_py_agent.cli.gateway_process import _run_gateway_service_loop
        from agent_py_agent.cli.models import GatewayRunContext

        stop_path = tmp_path / "gateway.stop"
        stop_path.write_text(
            json.dumps(
                {
                    "requested_at": 200.0,
                    "reason": "old gateway stop",
                    "target_process": {"host_id": "host-a", "pid": 111, "start_time": 10},
                }
            ),
            encoding="utf-8",
        )
        context = GatewayRunContext(
            agent=MagicMock(),
            paths=SimpleNamespace(stop_request=stop_path),
            config_path=tmp_path / "config.yaml",
            process_identity={"host_id": "host-a", "pid": 222, "start_time": 20},
            process_started_at=100.0,
        )

        def write_current_stop() -> None:
            stop_path.write_text(
                json.dumps(
                    {
                        "requested_at": 300.0,
                        "reason": "current gateway stop",
                        "target_process": {"host_id": "host-a", "pid": 222, "start_time": 20},
                    }
                ),
                encoding="utf-8",
            )

        timer = threading.Timer(0.05, write_current_stop)
        timer.start()
        try:
            report = _run_gateway_service_loop(context)
        finally:
            timer.cancel()

        assert report["stop_request"]["reason"] == "current gateway stop"

    def test_gateway_setup_rejects_overlapping_live_process(self, tmp_path: Path):
        """直接 gateway run 不能覆盖仍存活的旧 Gateway PID 记录。"""
        from agent_py_agent.cli.gateway_process import _cmd_gateway_run_setup

        with patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=98765):
            with pytest.raises(RuntimeError, match="gateway already running pid=98765"):
                _cmd_gateway_run_setup(MagicMock(), SimpleNamespace(pid=tmp_path / "gateway.pid"))


class TestCmdGatewayLogs:
    """测试 cmd_gateway_logs 命令。"""

    def test_gateway_logs_no_file(self, tmp_path: Path):
        """日志文件不存在时返回失败，提醒调用方没有可读日志。"""
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
            assert result == 1

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


class TestGatewayServiceWorkingDirectory:
    """测试常驻服务不会从源码目录启动并遮蔽已安装 wheel。"""

    def test_service_working_directory_uses_my_agent_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from agent_py_agent.cli import gateway_service

        home = tmp_path / "agent-home"
        monkeypatch.setenv("MY_AGENT_HOME", str(home))
        monkeypatch.setattr(gateway_service, "_get_config_path", lambda: str(tmp_path / "missing.yaml"))

        assert gateway_service._service_working_directory() == home / "service-cwd"

    def test_service_files_use_neutral_working_directory(self, tmp_path: Path):
        from agent_py_agent.cli import gateway_service

        service_cwd = tmp_path / "agent-home" / "service-cwd"
        with patch.object(gateway_service, "_service_working_directory", return_value=service_cwd):
            systemd = gateway_service.generate_systemd_unit_text()
            launchd = gateway_service.generate_launchd_plist_text()

        assert f"WorkingDirectory={service_cwd}" in systemd
        assert f"<string>{service_cwd}</string>" in launchd
        assert f"WorkingDirectory={gateway_service.PROJECT_ROOT}" not in systemd


# LLM: 就绪判据必须区分"本次启动的这一代"和"上一代/同 PID 的陈旧记录"：pid+status 相同不足以证明。
# 这两个入口现在从 agent.gateway_parts.status_rendering 复用唯一实现，gateway_process 只做导出。
# 函数用途: 验证 not_before 代际校验与字段缺失时的向后兼容。
def test_gateway_ready_requires_current_generation(tmp_path) -> None:
    from agent_py_agent.cli.gateway_process import (
        _gateway_ready_for_pid,
        _record_is_current_generation,
    )

    state = tmp_path / "state.json"
    heartbeat = tmp_path / "heartbeat.json"
    paths = SimpleNamespace(state=state, heartbeat=heartbeat, pid=tmp_path / "pid.json")
    spawned_at = 1_000.0

    def write(payload):
        state.write_text(json.dumps(payload), encoding="utf-8")

    # 陈旧记录：同 PID、running，但 started_at 早于本次启动 → 不算就绪
    write({"pid": 4242, "status": "running", "started_at": spawned_at - 60})
    assert _gateway_ready_for_pid(paths, 4242, not_before=spawned_at) is False
    assert _gateway_ready_for_pid(paths, 4242) is True  # 不传代际时保持原语义

    # 本次启动的这一代 → 就绪
    write({"pid": 4242, "status": "running", "started_at": spawned_at + 0.05})
    assert _gateway_ready_for_pid(paths, 4242, not_before=spawned_at) is True

    # 父进程写盘时差在容差内，不能把刚发布的一代误判成陈旧
    assert _record_is_current_generation({"started_at": spawned_at - 1.0}, spawned_at) is True
    # 缺字段的旧格式按"无法证明陈旧"处理（向后兼容）
    assert _record_is_current_generation({}, spawned_at) is True
    assert _record_is_current_generation({"started_at": "bad"}, spawned_at) is True


# ---------------------------------------------------------------------------
# 统一就绪判据（唯一权威）：真实 tmp 状态文件 + fake 进程，不启动真 Gateway、不碰用户状态目录
# ---------------------------------------------------------------------------


# LLM: 就绪判据的受控验收只允许写真实结构化文件（PID 记录 / state / heartbeat），不注入判据替身。
# 函数用途: 构造测试专用 Gateway 路径合同，全部落在 tmp 目录内。
def _readiness_paths(tmp_path: Path):
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

    return GatewayPaths(
        root=tmp_path / "gateway",
        pid=tmp_path / "gateway" / "gateway.pid",
        adapter_pid=tmp_path / "gateway" / "adapter.pid",
        state=tmp_path / "gateway" / "state.json",
        heartbeat=tmp_path / "gateway" / "heartbeat.json",
        stop_request=tmp_path / "gateway" / "stop.request",
        log=tmp_path / "gateway" / "gateway.log",
        inbox=tmp_path / "gateway" / "requests" / "pending",
        processing=tmp_path / "gateway" / "requests" / "processing",
        done=tmp_path / "gateway" / "requests" / "done",
        failed=tmp_path / "gateway" / "requests" / "failed",
        responses=tmp_path / "gateway" / "responses",
        history=tmp_path / "gateway" / "gateway_requests.jsonl",
    )


# LLM: PID 记录必须带真实进程出生时间，否则 get_running_pid_report 会按 PID 复用把记录当陈旧清掉；
#   updated_at 是本次启动的代际锚点，判据用它排除上一代残留。
# 函数用途: 为指定进程写一份可核验的 PID 记录。
def _write_readiness_pid_record(paths, pid: int, *, anchor_at: float | None = None) -> None:
    import time as time_module
    from datetime import datetime, timezone

    from agent_py_agent.agent.gateway_parts.daemon_metadata import _get_process_start_time

    paths.root.mkdir(parents=True, exist_ok=True)
    paths.pid.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "kind": "my-agent-gateway",
                "start_time": _get_process_start_time(int(pid)),
                "updated_at": datetime.fromtimestamp(
                    anchor_at if anchor_at is not None else time_module.time(),
                    timezone.utc,
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# LLM: state/heartbeat 是判据唯一读取的结构化来源；测试只写文件，不替换判定函数。
# 函数用途: 写一份 Gateway 状态记录（pid/status/started_at）。
def _write_readiness_status(
    paths,
    *,
    pid: int,
    status: str,
    started_at: float | None = None,
    heartbeat: bool = False,
) -> None:
    import time as time_module

    paths.root.mkdir(parents=True, exist_ok=True)
    target = paths.heartbeat if heartbeat else paths.state
    target.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "status": status,
                "started_at": time_module.time() if started_at is None else started_at,
            }
        ),
        encoding="utf-8",
    )


# LLM: fake 进程只提供 spawn 方的存活事实（pid + poll），不冒充 Gateway 进程本身。
# 类用途: 模拟 Popen 形状的子进程，用于验证 gateway start 的等待分型。
class _FakeSpawnedProcess:
    # LLM: exit_code=None 表示仍存活；poll 返回非 None 表示已退出。
    # 函数用途: 记录伪造 PID 与退出码。
    def __init__(self, pid: int, *, exit_code: int | None = None) -> None:
        self.pid = int(pid)
        self.exit_code = exit_code

    # LLM: 与 Popen.poll() 同语义：None 表示进程仍在运行。
    # 函数用途: 返回伪造的退出码。
    def poll(self):
        return self.exit_code


# LLM: 四态判据是唯一权威：alive/starting/ready/failed 只能来自 gateway_readiness，不允许调用方各写一套。
# 函数用途: 验证进程存活事实与 state 记录如何组合成四种结构化状态。
def test_readiness_authority_reports_starting_ready_failed_and_stopped(tmp_path: Path) -> None:
    import os

    from agent_py_agent.agent.gateway_parts.status_rendering import gateway_readiness

    paths = _readiness_paths(tmp_path)
    _write_readiness_pid_record(paths, os.getpid())

    _write_readiness_status(paths, pid=os.getpid(), status="starting")
    starting = gateway_readiness(paths)
    assert (starting.state, starting.reason) == ("starting", "GATEWAY_STARTING")
    assert starting.ready is False and starting.process_alive is True
    assert starting.source == "state"

    _write_readiness_status(paths, pid=os.getpid(), status="running")
    ready = gateway_readiness(paths)
    assert (ready.state, ready.reason) == ("ready", "GATEWAY_READY")
    assert ready.ready is True and ready.pid == os.getpid()

    _write_readiness_status(paths, pid=os.getpid(), status="failed")
    failed = gateway_readiness(paths)
    assert (failed.state, failed.reason) == ("failed", "GATEWAY_START_FAILED")
    assert failed.ready is False

    paths.pid.unlink()
    paths.state.unlink()
    stopped = gateway_readiness(paths)
    assert (stopped.state, stopped.reason) == ("stopped", "GATEWAY_NOT_READY")
    assert stopped.process_alive is False and stopped.pid is None


# LLM: 发布顺序保持"HTTP bind → heartbeat → state"，所以 heartbeat 的 running 与 state 的 running 同等可信，
#   state 还停在 starting 时也不能把已 bind 的网关判成未就绪。这条"覆盖 starting"是唯一允许的覆盖方向：
#   state 一旦越过 starting（failed / interrupted / http_server_error），它的结论就是权威，
#   后台 heartbeat 的周期 running 不许把它改写成 ready（见 test_gateway_readiness_generation.py）。
# 函数用途: 验证 state/heartbeat 交叉的边界语义没有被新判据改掉。
def test_readiness_heartbeat_running_is_enough_when_state_still_starting(tmp_path: Path) -> None:
    import os

    from agent_py_agent.agent.gateway_parts.status_rendering import gateway_readiness

    paths = _readiness_paths(tmp_path)
    _write_readiness_pid_record(paths, os.getpid())
    _write_readiness_status(paths, pid=os.getpid(), status="starting")
    _write_readiness_status(paths, pid=os.getpid(), status="running", heartbeat=True)

    readiness = gateway_readiness(paths)

    assert readiness.ready is True
    assert readiness.source == "heartbeat"
    assert readiness.status == "running"

    # 边界：state 越过 starting 后，同一个 running heartbeat 不得再放行。
    _write_readiness_status(paths, pid=os.getpid(), status="failed")
    failed = gateway_readiness(paths)

    assert failed.ready is False
    assert (failed.state, failed.reason) == ("failed", "GATEWAY_START_FAILED")
    assert failed.source == "state"


# LLM: 陈旧代际必须由结构化时间字段拒绝：PID 记录锚点（本次启动）比 state.started_at 新 600s 时，
#   同 PID 的 running 记录属于上一代，不得算 ready。
# 函数用途: 验证 gateway_readiness 使用 PID 记录锚点做代际过滤。
def test_readiness_rejects_state_older_than_pid_record_generation(tmp_path: Path) -> None:
    import os
    import time

    from agent_py_agent.agent.gateway_parts.status_rendering import (
        _gateway_ready_for_pid,
        gateway_readiness,
    )

    paths = _readiness_paths(tmp_path)
    anchor = time.time()
    _write_readiness_pid_record(paths, os.getpid(), anchor_at=anchor)
    _write_readiness_status(paths, pid=os.getpid(), status="running", started_at=anchor - 600)

    assert gateway_readiness(paths).ready is False
    # 只按记录层看它确实是 running：拒绝它的是代际判据，不是别的条件。
    assert _gateway_ready_for_pid(paths, os.getpid()) is True
    assert _gateway_ready_for_pid(paths, os.getpid(), not_before=anchor) is False


# LLM: 等待结局必须用结构化 state+reason 分型：超时但仍在启动 ≠ 从未启动 ≠ 进程已退出 ≠ 服务失败。
# 函数用途: 验证四种等待结局的可区分性。
def test_wait_for_readiness_distinguishes_timeout_failure_and_exit(tmp_path: Path) -> None:
    import os

    from agent_py_agent.agent.gateway_parts.status_rendering import wait_for_gateway_readiness

    paths = _readiness_paths(tmp_path)
    _write_readiness_pid_record(paths, os.getpid())
    _write_readiness_status(paths, pid=os.getpid(), status="starting")

    starting_timeout = wait_for_gateway_readiness(paths, 0.3)
    assert (starting_timeout.state, starting_timeout.reason) == ("timeout", "GATEWAY_START_TIMEOUT")
    assert starting_timeout.last.state == "starting"
    assert starting_timeout.observed_alive is True
    assert starting_timeout.ready is False

    _write_readiness_status(paths, pid=os.getpid(), status="failed")
    service_failed = wait_for_gateway_readiness(paths, 0.3)
    assert (service_failed.state, service_failed.reason) == ("failed", "GATEWAY_START_FAILED")

    paths.state.unlink()
    paths.pid.unlink()
    never_started = wait_for_gateway_readiness(paths, 0.3)
    assert (never_started.state, never_started.reason) == ("timeout", "GATEWAY_NOT_READY")
    assert never_started.last.state == "stopped"
    assert never_started.observed_alive is False

    _write_readiness_pid_record(paths, os.getpid())
    _write_readiness_status(paths, pid=os.getpid(), status="starting")
    process_exited = wait_for_gateway_readiness(
        paths,
        5.0,
        process=_FakeSpawnedProcess(os.getpid(), exit_code=1),
    )
    assert (process_exited.state, process_exited.reason) == ("failed", "GATEWAY_PROCESS_EXITED")
    assert process_exited.elapsed_seconds < 1.0


# LLM: cmd_gateway_start 的等待必须复用唯一判据：就绪来自本代 running 记录，子进程死亡走 poll() 快速失败；
#   预算、发布顺序和退出码语义保持不变。
# 函数用途: 验证 gateway start 等待在 starting 记录上超时、在 running 记录上就绪、在子进程死亡时快速失败。
def test_gateway_start_wait_reuses_canonical_readiness(tmp_path: Path) -> None:
    import os

    from agent_py_agent.cli.gateway_process import _wait_for_gateway_start_ready

    paths = _readiness_paths(tmp_path)
    _write_readiness_pid_record(paths, os.getpid())
    _write_readiness_status(paths, pid=os.getpid(), status="starting")
    alive_process = _FakeSpawnedProcess(os.getpid())

    assert _wait_for_gateway_start_ready(paths, alive_process, timeout=0.3) is False

    _write_readiness_status(paths, pid=os.getpid(), status="running")
    assert _wait_for_gateway_start_ready(paths, alive_process, timeout=0.3) is True

    dead_process = _FakeSpawnedProcess(os.getpid(), exit_code=1)
    assert _wait_for_gateway_start_ready(paths, dead_process, timeout=5.0) is False


# LLM: 失败分型只改呈现，不改退出码：gateway start 仍返回 2，但 stderr 必须给出结构化 reason。
# 函数用途: 验证子进程退出时 cmd_gateway_start 返回 2 且打印 GATEWAY_PROCESS_EXITED。
def test_cmd_gateway_start_failure_keeps_exit_code_two_with_typed_reason(tmp_path: Path, capsys) -> None:
    from agent_py_agent.cli.gateway_process import cmd_gateway_start

    paths = _readiness_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        root=tmp_path,
        config=SimpleNamespace(gateway_stop_timeout=5, gateway_ready_timeout_seconds=0.2),
    )
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), force=False)
    mock_process = _FakeSpawnedProcess(99999, exit_code=1)

    with patch("agent_py_agent.cli.gateway_process.make_agent", return_value=agent), \
         patch("agent_py_agent.cli.gateway_process.gateway_paths", return_value=paths), \
         patch("agent_py_agent.cli.gateway_process.get_running_pid", return_value=None), \
         patch("agent_py_agent.cli.gateway_process._get_process_start_time", return_value=None), \
         patch("agent_py_agent.cli.gateway_process._wait_for_gateway_start_ready", return_value=False), \
         patch("subprocess.Popen", return_value=mock_process):
        result = cmd_gateway_start(args)

    assert result == 2
    stderr = capsys.readouterr().err
    assert "did not become ready before timeout" in stderr
    assert "reason=GATEWAY_PROCESS_EXITED" in stderr
