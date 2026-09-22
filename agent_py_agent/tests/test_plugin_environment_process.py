"""环境命令的取消与退出回执替身测试，不启动插件或真实模型。"""

import os
import subprocess
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_environment_process as module
from agent_py_agent.agent.tooling.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt


def test_preparation_scrubs_secrets_and_all_runtime_injection_variables(tmp_path, monkeypatch):
    supplied = {
        "MINIMAX_API_KEY": "placeholder", "SOME_TOKEN": "placeholder", "HOME": str(tmp_path),
        "PATH": "/usr/bin", "PYTHONPATH": "external", "PIP_INDEX_URL": "https://example.invalid",
        "LD_PRELOAD": "external", "DYLD_INSERT_LIBRARIES": "external", "CONDA_PREFIX": "external",
        "UV_INDEX": "external", "VIRTUAL_ENV": "external", "__PYVENV_LAUNCHER__": "external",
    }
    monkeypatch.setattr(module.os, "environ", supplied)
    result = module.preparation_environment(tmp_path / "temporary")
    assert result == {
        "HOME": str(tmp_path), "PATH": "/usr/bin", "PIP_CONFIG_FILE": os.devnull,
        "TMPDIR": str(tmp_path / "temporary"), "TEMP": str(tmp_path / "temporary"), "TMP": str(tmp_path / "temporary"),
    }
    assert supplied["MINIMAX_API_KEY"] == "placeholder"


@pytest.mark.parametrize("confirmed", [True, False])
def test_cancellation_uses_original_token_and_exact_birth_identity(tmp_path, monkeypatch, confirmed):
    token = CancellationToken()
    calls = []

    def communicate(timeout):
        token.cancel("test-control")
        raise subprocess.TimeoutExpired("host-preparation", timeout)

    process = SimpleNamespace(pid=123456, returncode=None, communicate=communicate)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(module, "capture_process_birth_token", lambda pid: "frozen-birth")

    def terminate(pid, proc, **kwargs):
        calls.append((pid, proc, kwargs))
        return ProcessTerminationReceipt("test", confirmed, -15 if confirmed else None, 1)

    monkeypatch.setattr(module, "terminate_process_tree", terminate)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled if confirmed else module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=module.time.monotonic() + 5)
    assert calls == [(123456, process, {"expected_birth_token": "frozen-birth"})]
    if not confirmed:
        assert error.value.reason == "process_exit_unknown" and error.value.exit_confirmed is False


def test_deadline_after_spawn_stops_exact_process(tmp_path, monkeypatch):
    clock = iter((0, 2))
    process = SimpleNamespace(pid=123456, returncode=None)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(module, "capture_process_birth_token", lambda pid: "frozen")
    stopped = []
    monkeypatch.setattr(module, "terminate_process_tree", lambda pid, proc, **kw: (
        stopped.append((pid, kw)), ProcessTerminationReceipt("test", True, -15, 1)
    )[1])
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=1)
    assert error.value.reason == "preparation_timeout" and error.value.exit_confirmed
    assert stopped == [(123456, {"expected_birth_token": "frozen"})]


def test_cancelled_before_spawn_creates_no_process(tmp_path, monkeypatch):
    token = CancellationToken()
    token.cancel()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: pytest.fail("不得启动"))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=100)


def test_host_probe_and_command_failure_use_bounded_output(tmp_path, monkeypatch):
    observed = []
    process = SimpleNamespace(pid=123456, returncode=0, communicate=lambda **kw: (b'{"prefix":"private"}', None))

    def popen(argv, **kwargs):
        observed.append((argv, kwargs))
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "capture_process_birth_token", lambda pid: "frozen")
    result = module.run_environment_process(("host-python", "-I"), cwd=tmp_path, environment={}, deadline=module.time.monotonic() + 5, capture=True)
    assert result == b'{"prefix":"private"}'
    assert observed[0][1]["start_new_session"] is True
    assert observed[0][1]["stdin"] == subprocess.DEVNULL
    assert observed[0][1]["stderr"] == subprocess.DEVNULL
    process.returncode = 1
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=module.time.monotonic() + 5)
    assert error.value.reason == "preparation_command"


@pytest.mark.parametrize("failure_stage,confirmed", [("birth", False), ("wait", True), ("cleanup", False)])
def test_post_spawn_failures_always_return_exit_evidence(tmp_path, monkeypatch, failure_stage, confirmed):
    process = SimpleNamespace(pid=123456, returncode=None)
    cleanup = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)

    def birth(pid):
        if failure_stage == "birth":
            raise OSError("identity read failed")
        return "frozen"

    def wait(*_):
        raise OSError("pipe failed")

    def terminate(pid, proc, **kwargs):
        cleanup.append((pid, kwargs))
        if failure_stage == "cleanup":
            raise OSError("cleanup failed")
        return ProcessTerminationReceipt("test", confirmed, -15 if confirmed else None, 1)

    monkeypatch.setattr(module, "capture_process_birth_token", birth)
    monkeypatch.setattr(module, "_wait_for_preparation", wait)
    monkeypatch.setattr(module, "terminate_process_tree", terminate)
    with pytest.raises(module.EnvironmentPreparationError) as error:
        module.run_environment_process(("host-python",), cwd=tmp_path, environment={}, deadline=module.time.monotonic() + 5)
    assert cleanup == [(123456, {"expected_birth_token": "" if failure_stage == "birth" else "frozen"})]
    assert error.value.exit_confirmed is confirmed
    assert error.value.reason == ("preparation_io" if confirmed else "process_exit_unknown")
