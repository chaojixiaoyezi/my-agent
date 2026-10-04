"""候选异常、并发安装与解释器漂移；只替换外部进程边界，保留真实 Store/HostCommand。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_configuration import PluginConfigureRequest
from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.tooling.mcp_client import MCPError
from agent_py_agent.tests.test_plugin_legacy_drift import change_target, permission_target
from agent_py_agent.tests.test_plugin_legacy_management import (
    confirm,
    fake_processes,
    installed_manager,
    preview,
)


def candidate_boundary(monkeypatch, stage, *, mutation=None):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            calls.append("client")
            if stage == "constructor" and mutation:
                mutation()

        def start(self):
            calls.append("start")
            if stage == "start_error":
                raise MCPError("fixture start failed")
            return object()

        def discover_tools(self, transport):
            calls.append("discover")
            if stage == "discover_error":
                raise MCPError("fixture catalog failed")
            if stage == "discover" and mutation:
                mutation()
            return []

        def stop(self):
            calls.append("stop")
            if stage == "stop" and mutation:
                mutation()
            return SimpleNamespace(confirmed=stage != "exit_unknown", record={"session_id": "fake"}, terminations=())

    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.PluginMCPClient", Client)
    return calls


@pytest.mark.parametrize("stage", ["start_error", "discover_error", "exit_unknown"])
def test_candidate_failure_never_publishes_and_replay_does_not_restart(tmp_path, monkeypatch, stage):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    prepared = fake_processes(monkeypatch)
    calls = candidate_boundary(monkeypatch, stage)
    first = preview(service)
    result = confirm(service, first)
    assert result["state"] == ("outcome_unknown" if stage == "exit_unknown" else "failed"), result
    entry = service.installations.snapshot()[0]
    assert not entry.enabled and entry.activation.phase == "preparing"
    assert calls[-1] == "stop" and prepared == ["prepare"]
    before = list(calls)
    replay = confirm(service, first, "resend")
    assert replay["state"] == result["state"] and calls == before and prepared == ["prepare"]


@pytest.mark.parametrize("stage", ["constructor", "discover", "stop"])
def test_candidate_phase_drift_never_publishes_and_stops_exact_candidate(tmp_path, monkeypatch, stage):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, "read_inode")
    first = preview(service, flags)
    fake_processes(monkeypatch)
    calls = candidate_boundary(monkeypatch, stage, mutation=lambda: change_target(path, "read_inode"))
    result = confirm(service, first)
    assert result["state"] == "failed" and result["details"]["reason"] == "legacy_permission_changed", result
    assert service.installations.snapshot()[0].activation.phase == "preparing"
    assert calls[-1] == "stop" and calls.count("stop") == 1
    if stage == "constructor":
        assert calls == ["client", "stop"]


def test_configuration_changes_after_constructor_never_prepares_stale_installation(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_management import manager
    from agent_py_agent.tests.test_plugin_package import _bundle

    service, source = manager(tmp_path, legacy_sandbox_default=False)
    source.write_bytes(_bundle(change=lambda manifest: manifest.update(settings_schema={
        "type": "object", "properties": {"mode": {"type": "string"}}, "additionalProperties": False})))
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert installed["state"] == "succeeded"
    first = preview(service)
    original = PluginEnableTool.execute
    calls = fake_processes(monkeypatch)

    def execute(tool, params):
        entry = service.installations.snapshot()[0]
        service.installations.configure(PluginConfigureRequest(
            entry.manifest.plugin_id, entry.package_sha256, "concurrent-configure", entry.revision, '{"mode":"strict"}'))
        return original(tool, params)

    monkeypatch.setattr(PluginEnableTool, "execute", execute)
    result = confirm(service, first)
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict", result
    assert service.installations.snapshot()[0].activation is None and calls == []


def test_interpreter_changes_during_preparation_never_starts_candidate(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_any_language import (
        _SERVER,
        _declaration,
        _fake_interpreter,
        _installed,
    )

    interpreter = _fake_interpreter(tmp_path, monkeypatch)
    original = _installed(tmp_path, _declaration("interpreter", interpreter.name), {"server.py": _SERVER})
    service = PluginManagement(replace(original.context, legacy_sandbox_default=False))
    first = service.command("/plugins enable sample-any", revision=service.catalog().revision, request_id="preview")
    assert first["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED"
    calls = fake_processes(monkeypatch)

    def prepare(*args):
        calls.append("prepare")
        interpreter.write_bytes(b"different interpreter, never executed")

    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.prepare_plugin_environment", prepare)
    result = confirm(service, first)
    assert result["state"] == "failed" and result["details"]["reason"] == "legacy_permission_changed", result
    assert not service.installations.snapshot()[0].enabled and calls == ["prepare"]
