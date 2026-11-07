"""S2-W5B: status reports live interpreter state, independently of build flavour.

Run: python -m pytest agent_py_agent/tests/ft/test_ft_interpreter_status.py
The status request uses the production payload builder; unrelated board I/O is isolated.
"""
from __future__ import annotations

import sys
import sysconfig
from types import SimpleNamespace

import pytest

from agent_py_agent.cli import local_status_payload


@pytest.fixture
def status_context(tmp_path, monkeypatch):
    monkeypatch.setattr(local_status_payload, "shared_progress_for_board", lambda *a, **k: {})
    return local_status_payload.StatusPayloadContext(
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="ft-test"), root=tmp_path),
        paths=SimpleNamespace(root=tmp_path),
        local_stats={},
        board=SimpleNamespace(hot_list=[], recent=[], summary={}),
        timeline=[],
        pid=None,
        alive=False,
        gateway_status="stopped",
        heartbeat_age=0,
        active_work_summary=None,
        request_counts={},
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_status_reports_live_gil_instead_of_build_flavour(status_context, monkeypatch, enabled):
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: enabled, raising=False)
    payload = local_status_payload.build_status_payload(status_context)
    assert "interpreter" in payload
    assert payload["interpreter"] == {
        "gil_enabled": enabled,
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    }


def test_status_reads_gil_again_for_each_payload(status_context, monkeypatch):
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    first = local_status_payload.build_status_payload(status_context)
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
    second = local_status_payload.build_status_payload(status_context)
    assert "interpreter" in first and "interpreter" in second
    assert first["interpreter"]["gil_enabled"] is True
    assert second["interpreter"]["gil_enabled"] is False
    assert first["interpreter"] is not second["interpreter"]


def test_status_on_pre314_interpreter_keeps_gil_enabled(status_context, monkeypatch):
    monkeypatch.delattr(sys, "_is_gil_enabled", raising=False)
    payload = local_status_payload.build_status_payload(status_context)
    assert "interpreter" in payload
    assert payload["interpreter"]["gil_enabled"] is True
    assert payload["interpreter"]["free_threaded_build"] is bool(
        sysconfig.get_config_var("Py_GIL_DISABLED")
    )
