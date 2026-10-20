# LLM: 仅用临时目录、原文件锁和fakecontext验证生命周期边界，不启动服务、Agent或模型。
# 模块用途: 保证Count0同锁与真实身份有限观察不能被误当MP闭封或迁移许可。
import copy
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import standalone_lifecycle as lifecycle
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    CoordinatorUnavailable,
    coordinator_guard,
    current_coordinator,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


def _context(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    identity = {"pid": os.getpid(), "hostname": "test-host", "start_time": "captured-birth"}
    return SimpleNamespace(paths=paths, process_identity=identity, process_started_at=123.0,
                           coordinator_worker_count=0, control_plane=None)


def _stopped(context):
    payload = {"status": "stopped", "pid": os.getpid(), "worker_count": 0,
               "process_identity": context.process_identity, "started_at": 123.0}
    write_json_file_atomic(context.paths.state, payload)
    return payload


def test_count0_holds_original_lock_without_registering_c(tmp_path):
    with lifecycle.standalone_lifecycle_guard(tmp_path) as held:
        held.require_held()
        assert held.path == tmp_path / "coordinator.lock"
        assert not os.get_inheritable(held.handle.fileno())
        assert current_coordinator(tmp_path, 2) is None
        with pytest.raises(lifecycle.StandaloneLifecycleUnavailable):
            with lifecycle.standalone_lifecycle_guard(tmp_path):
                pytest.fail("不能重叠生命周期")
    with lifecycle.standalone_lifecycle_guard(tmp_path):
        pass


@pytest.mark.skipif(os.name == "nt", reason="原多进程入口仅POSIX开放")
def test_count0_and_original_mp_guard_exclude_each_other(tmp_path):
    with lifecycle.standalone_lifecycle_guard(tmp_path):
        with pytest.raises(CoordinatorUnavailable):
            with coordinator_guard(tmp_path, 2):
                pytest.fail("Count0持锁时不能成为C")
    with coordinator_guard(tmp_path, 2):
        with pytest.raises(lifecycle.StandaloneLifecycleUnavailable):
            with lifecycle.standalone_lifecycle_guard(tmp_path):
                pytest.fail("C持锁时不能启动Count0")


def test_copied_or_closed_lock_object_is_not_authority(tmp_path):
    with lifecycle.standalone_lifecycle_guard(tmp_path) as held:
        copied = copy.copy(held)
        with pytest.raises(lifecycle.StandaloneLifecycleUnavailable):
            copied.require_held()
    with pytest.raises(lifecycle.StandaloneLifecycleUnavailable):
        held.require_held()


def test_replaced_lock_inode_is_rejected(tmp_path):
    with lifecycle.standalone_lifecycle_guard(tmp_path) as held:
        held.path.rename(tmp_path / "original.lock")
        held.path.write_text("")
        with pytest.raises(lifecycle.StandaloneLifecycleUnavailable):
            held.require_held()


def test_symlink_lock_is_rejected(tmp_path):
    target = tmp_path / "other.lock"
    target.write_text("")
    (tmp_path / "coordinator.lock").symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        with lifecycle.standalone_lifecycle_guard(tmp_path):
            pytest.fail("不能跟随锁软链接")


@pytest.mark.parametrize("joined", [False, True])
def test_normal_stop_keeps_captured_identity_but_never_claims_complete(tmp_path, joined):
    context = _context(tmp_path)
    with lifecycle.standalone_lifecycle_guard(context.paths.root) as held:
        before = _stopped(context)
        result = lifecycle.record_standalone_stop_observation(held, context, {"drain_complete": joined})
    assert {key: result[key] for key in before} == before
    observation = result["standalone_shutdown_observation"]
    assert observation["process_identity"] == context.process_identity
    assert observation["process_started_at"] == 123.0
    assert observation["gateway_loops_joined"] is joined
    assert observation["complete"] is False
    assert len(observation["unobserved_responsibilities"]) == 7
    assert not context.paths.pid.exists()
    assert "c_run_id" not in result and "closed" not in result


@pytest.mark.parametrize("invalid", ["failed", "foreign_identity", "coordinator"])
def test_nonmatching_stop_facts_cannot_publish(tmp_path, invalid):
    context = _context(tmp_path)
    with lifecycle.standalone_lifecycle_guard(context.paths.root) as held:
        before = _stopped(context)
        if invalid == "failed":
            before["status"] = "failed"
        elif invalid == "foreign_identity":
            before["process_identity"] = {"pid": os.getpid(), "start_time": "other"}
        else:
            context.control_plane = object()
        write_json_file_atomic(context.paths.state, before)
        original = context.paths.state.read_bytes()
        with pytest.raises(ValueError):
            lifecycle.record_standalone_stop_observation(held, context, {"drain_complete": True})
        assert context.paths.state.read_bytes() == original


@pytest.mark.parametrize("format_only", [False, True])
def test_state_change_between_read_and_transaction_is_not_overwritten(tmp_path, monkeypatch, format_only):
    context = _context(tmp_path)
    original_update = lifecycle.update_json_file_atomic

    def concurrent_update(path, update, **kwargs):
        if format_only:
            path.write_text(json.dumps(json.loads(path.read_text()), sort_keys=True))
        else:
            write_json_file_atomic(path, {"status": "running", "pid": 999})
        return original_update(path, update, **kwargs)

    monkeypatch.setattr(lifecycle, "update_json_file_atomic", concurrent_update)
    with lifecycle.standalone_lifecycle_guard(context.paths.root) as held:
        _stopped(context)
        with pytest.raises(ValueError, match="已变化"):
            lifecycle.record_standalone_stop_observation(held, context, {"drain_complete": True})
    if format_only:
        assert "standalone_shutdown_observation" not in json.loads(context.paths.state.read_text())
    else:
        assert json.loads(context.paths.state.read_text()) == {"status": "running", "pid": 999}
