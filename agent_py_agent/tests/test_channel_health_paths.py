"""真实原PID/state健康投影的显式paths入口，不创建Agent或更改daemon状态。"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent_py_agent.agent.gateway_parts import channel_health as health
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.tests.test_channel_health import _agent


@pytest.mark.parametrize("state", ["fresh", "stale", "dead", "corrupt"])
def test_paths_health_is_same_canonical_projection_without_agent(tmp_path, monkeypatch, state):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir()
    paths.adapter_pid.write_text(json.dumps({"pid": 999999999 if state == "dead" else os.getpid()}))
    updated = datetime.now(timezone.utc) - (
        timedelta(seconds=120) if state == "stale" else timedelta()
    )
    payload = {
        "state": "running",
        "updated_at": updated.isoformat(),
        "channels": [{"name": "feishu", "health": {"state": "healthy", "error_code": ""}}],
    }
    (paths.root / "adapter_state.json").write_text(
        "{broken" if state == "corrupt" else json.dumps(payload)
    )
    monkeypatch.setattr(health, "_utc_now_iso", lambda: "2026-10-17T00:00:00+00:00")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths.root.iterdir()}
    directory_mtime = paths.root.stat().st_mtime_ns
    result = health.adapter_runtime_health_for_paths(paths, cleanup_stale=False)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths.root.iterdir()} == before
    assert paths.root.stat().st_mtime_ns == directory_mtime
    assert result == health.adapter_runtime_health(_agent(tmp_path))
    if state == "fresh":
        assert result["feishu"]["state"] == "healthy"
    else:
        assert result["*"]["state"] == "unhealthy"


def test_paths_missing_does_not_create_or_infer_healthy(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "not-created")
    result = health.adapter_runtime_health_for_paths(paths, cleanup_stale=False)
    assert result["*"]["state"] == "unhealthy"
    assert not paths.root.exists()


def test_original_agent_stale_cleanup_is_retained_but_c_never_unlinks_pid(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir()
    paths.adapter_pid.write_text(json.dumps({"pid": 999999999}))
    before = paths.adapter_pid.read_bytes()
    assert (
        health.adapter_runtime_health_for_paths(paths, cleanup_stale=False)["*"]["state"]
        == "unhealthy"
    )
    assert paths.adapter_pid.read_bytes() == before
    assert health.adapter_runtime_health(_agent(tmp_path))["*"]["state"] == "unhealthy"
    assert not paths.adapter_pid.exists()


@pytest.mark.parametrize("fault", ["directory", "bad_pid", "reused_pid", "missing_state"])
def test_paths_unreadable_or_unknown_facts_never_become_healthy(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.gateway_parts import daemon_control

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir()
    paths.adapter_pid.write_text(json.dumps({"pid": os.getpid()}))
    state = paths.root / "adapter_state.json"
    if fault == "directory":
        state.mkdir()
    elif fault == "bad_pid":
        paths.adapter_pid.write_text("{broken")
    elif fault == "reused_pid":
        paths.adapter_pid.write_text(json.dumps({"pid": os.getpid(), "start_time": "old-birth"}))
        monkeypatch.setattr(daemon_control, "_get_process_start_time", lambda _pid: "new-birth")
    before = {
        p.name: (p.stat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
        for p in paths.root.iterdir()
    }
    result = health.adapter_runtime_health_for_paths(paths, cleanup_stale=False)
    assert result["*"]["state"] in {"unhealthy", "stopped"}
    assert {
        p.name: (p.stat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
        for p in paths.root.iterdir()
    } == before
    assert not (paths.root / "adapter_state.json.lock").exists()
