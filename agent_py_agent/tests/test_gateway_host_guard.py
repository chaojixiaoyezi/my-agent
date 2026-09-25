"""托管自停闸：Gateway 托管的工具进程不能停止或重启托管自己的那台 Gateway。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.tooling.shell import _subprocess_text_env
from agent_py_agent.cli import gateway_process
from agent_py_agent.cli.gateway_host_guard import (
    HOSTING_GATEWAY_PID_ENV,
    mark_hosting_gateway_process,
    refuse_stopping_hosting_gateway,
)

_HOST_PID = 43210


def _agent_and_paths(tmp_path):
    agent = MagicMock()
    agent.config = SimpleNamespace(gateway_stop_timeout=5, gateway_port=0)
    paths = MagicMock()
    paths.root, paths.pid, paths.stop_request = tmp_path, tmp_path / "gateway.pid", tmp_path / "stop.json"
    return agent, paths


def test_mark_writes_own_pid_and_children_inherit_it_after_scrub(monkeypatch, tmp_path):
    monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, "1")
    mark_hosting_gateway_process()
    assert os.environ[HOSTING_GATEWAY_PID_ENV] == str(os.getpid())
    assert _subprocess_text_env()[HOSTING_GATEWAY_PID_ENV] == str(os.getpid())
    assert _subprocess_text_env(tmp_path)[HOSTING_GATEWAY_PID_ENV] == str(os.getpid()), "降权擦洗后仍保留"


def test_refuses_only_the_exact_hosting_gateway(monkeypatch, capsys):
    monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, str(_HOST_PID))
    assert refuse_stopping_hosting_gateway(_HOST_PID) is True
    assert "会切断正在执行这条命令的对话回合" in capsys.readouterr().err
    assert refuse_stopping_hosting_gateway(_HOST_PID + 1) is False
    for value in ("", "abc", "0", "-5"):
        monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, value)
        assert refuse_stopping_hosting_gateway(_HOST_PID) is False
    monkeypatch.delenv(HOSTING_GATEWAY_PID_ENV)
    assert refuse_stopping_hosting_gateway(_HOST_PID) is False


def test_stop_and_restart_refuse_before_any_stop_request(monkeypatch, tmp_path):
    monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, str(_HOST_PID))
    agent, paths = _agent_and_paths(tmp_path)
    args = SimpleNamespace(config="c.yaml", reason="", timeout=None, kill=True, ready_timeout=None)
    with patch.object(gateway_process, "make_agent", return_value=agent), \
         patch.object(gateway_process, "gateway_paths", return_value=paths), \
         patch.object(gateway_process, "get_running_pid", return_value=_HOST_PID), \
         patch.object(gateway_process, "write_targeted_gateway_stop_request") as stop_request, \
         patch.object(gateway_process, "terminate_pid") as terminate, \
         patch.object(gateway_process, "cmd_gateway_start") as start:
        assert gateway_process.cmd_gateway_stop(args) == 2
        assert gateway_process.cmd_gateway_restart(args) == 2
    stop_request.assert_not_called()
    terminate.assert_not_called()
    start.assert_not_called()


def test_forced_start_refuses_to_replace_the_hosting_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, str(_HOST_PID))
    agent, paths = _agent_and_paths(tmp_path)
    with patch.object(gateway_process, "make_agent", return_value=agent), \
         patch.object(gateway_process, "gateway_paths", return_value=paths), \
         patch.object(gateway_process, "get_running_pid", return_value=_HOST_PID), \
         patch.object(gateway_process, "is_pid_alive", return_value=True), \
         patch.object(gateway_process, "_write_gateway_stop_request") as stop_request, \
         patch.object(gateway_process, "_spawn_gateway_process") as spawn:
        assert gateway_process.cmd_gateway_start(SimpleNamespace(config="c.yaml", force=True, ready_timeout=None)) == 2
    stop_request.assert_not_called()
    spawn.assert_not_called()


def test_other_gateway_is_still_stoppable_from_a_hosted_tool(monkeypatch, tmp_path):
    monkeypatch.setenv(HOSTING_GATEWAY_PID_ENV, str(_HOST_PID))
    agent, paths = _agent_and_paths(tmp_path)
    args = SimpleNamespace(config="c.yaml", reason="", timeout=1, kill=False)
    with patch.object(gateway_process, "make_agent", return_value=agent), \
         patch.object(gateway_process, "gateway_paths", return_value=paths), \
         patch.object(gateway_process, "get_running_pid", return_value=_HOST_PID + 7), \
         patch.object(gateway_process, "write_targeted_gateway_stop_request", return_value={"ok": True}) as stop_request, \
         patch.object(gateway_process, "log_gateway_event"), \
         patch.object(gateway_process, "wait_for_pid_exit", return_value=True):
        assert gateway_process.cmd_gateway_stop(args) == 0
    stop_request.assert_called_once()
