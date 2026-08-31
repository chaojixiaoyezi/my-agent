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
