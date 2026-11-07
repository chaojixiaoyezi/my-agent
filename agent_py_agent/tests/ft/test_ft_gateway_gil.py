"""GIL 调优与真实启动入口的观察；3.12 沿用原有调度行为。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.cli import gateway_process


@pytest.mark.parametrize("enabled", [False, True, None])
def test_switch_interval_only_changes_when_gil_enabled(monkeypatch, enabled):
    calls = []
    monkeypatch.setattr(gateway_process.sys, "setswitchinterval", calls.append)
    if enabled is None:
        monkeypatch.delattr(gateway_process.sys, "_is_gil_enabled", raising=False)
    else:
        monkeypatch.setattr(gateway_process.sys, "_is_gil_enabled", lambda: enabled, raising=False)
    gateway_process._tune_gateway_gil_switch_interval()
    assert calls == ([] if enabled is False else [0.001])


@pytest.mark.parametrize("failure", [AttributeError, ValueError])
def test_unavailable_switch_interval_api_remains_optional(monkeypatch, failure):
    def unavailable(_interval):
        raise failure("unavailable")

    monkeypatch.setattr(gateway_process.sys, "_is_gil_enabled", lambda: True, raising=False)
    monkeypatch.setattr(gateway_process.sys, "setswitchinterval", unavailable)
    gateway_process._tune_gateway_gil_switch_interval()


def test_gil_observation_records_runtime_and_interpreter_build(monkeypatch, caplog):
    monkeypatch.setattr(gateway_process.sys, "_is_gil_enabled", lambda: False, raising=False)
    observe = getattr(gateway_process, "_record_gil_state", None)
    assert callable(observe), "gateway has no GIL observation"
    with caplog.at_level(logging.INFO):
        observe("startup")
    record = next(record for record in caplog.records if getattr(record, "gil_stage", None) == "startup")
    assert record.gil_enabled is False
    assert isinstance(record.gil_disabled_build, bool)


def test_gil_observation_works_without_private_api(monkeypatch, caplog):
    monkeypatch.delattr(gateway_process.sys, "_is_gil_enabled", raising=False)
    observe = getattr(gateway_process, "_record_gil_state", None)
    assert callable(observe), "gateway has no GIL observation"
    with caplog.at_level(logging.INFO):
        observe("after_first_request")
    record = next(record for record in caplog.records if getattr(record, "gil_stage", None) == "after_first_request")
    assert record.gil_enabled is True


def test_startup_observation_precedes_coordinated_thread_handoff(monkeypatch):
    from agent_py_agent.cli import gateway_coordinated_run

    stages = []
    monkeypatch.setattr(gateway_process, "_record_gil_state", stages.append, raising=False)
    monkeypatch.setattr(gateway_process, "_tune_gateway_gil_switch_interval", lambda: None)

    def start(request):
        assert stages == ["startup"]
        return request

    monkeypatch.setattr(gateway_coordinated_run, "start_coordinated_loops", start)
    request = SimpleNamespace(context=SimpleNamespace(control_plane=object()))
    assert gateway_process._cmd_gateway_run_threads(request) is request
