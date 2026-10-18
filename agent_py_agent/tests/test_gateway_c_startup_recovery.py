# LLM: 正式C启动恢复只用原C/Root/P3及规范旧processing，不启动物理W、服务或模型；假的线程consumer只隔离后续循环出生。
# 模块用途: 验证启动真正恢复旧回合、原预算不重置，以及不消费不支持的计划重启标记。
import os
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.restart_service import (
    COMPLETED_SCHEMA,
    consume_restart_marker,
    restart_completed_path,
)
from agent_py_agent.cli import gateway_coordinated_run as coordinated
from agent_py_agent.cli import gateway_process as api
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_cold_processing_recovery import cold_case


# LLM: 只运输fixture实际C/原Root，不补Agent字段或签发新的owner权限。
# 函数用途: 为原启动门保留同一控制面、路径和真实资源根。
def context_for(original, control, tmp_path):
    return GatewayRunContext(None, original.gate.paths, tmp_path / "config.yaml",
        coordinator_worker_count=1, control_plane=control, drain_runtime=GatewayDrainRuntime(control))


def test_formal_c_prepare_recovers_processing_before_threads_without_agent(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch, with_input=True) as (original, control, _gate, _ctx):
        context = context_for(original, control, tmp_path)
        original_start = type(control).start_business
        monkeypatch.setattr(type(control), "start_business",
            lambda current, _factory: original_start(current, lambda: None))
        payload = read_json_file(original.claim.path)
        payload["lease_process_identity"]["pid"] = 999999999
        payload["processing_failure_count"] = 2
        write_json_file_atomic(original.claim.path, payload)
        events = []
        monkeypatch.setattr(api, "_start_gateway_http", lambda *_args: None)

        def threads(request):
            events.append((request.requeued, request.failed, original.claim.path.exists()))
            return request.stop_event, None, None, None, request.http_server

        monkeypatch.setattr(api, "_cmd_gateway_run_threads", threads)
        cleanup = coordinated._prepare_coordinated_resources(context)
        assert cleanup.context.agent is None
        assert events == [(1, 0, False)]
        payload = read_json_file(context.paths.inbox / original.claim.path.name)
        assert payload["id"] == original.claim.request_id
        assert payload.get("processing_failure_count") == 2
        assert payload["active_turn_recovery"]["unplanned_resume_count"] == 1
        assert payload["not_before_at"] > time.time()
        from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
            _read_reservation,
            reservation_file,
        )
        assert _read_reservation(reservation_file(context.paths, original.claim.request_id))["state"] == "occupied"


def test_original_marker_can_be_peeked_without_consuming(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    paths = gateway_paths_from_root(tmp_path)
    marker = {"schema": COMPLETED_SCHEMA, "old_pid": 999999999, "drained_at": time.time()}
    target = restart_completed_path(paths)
    write_json_file_atomic(target, marker)
    before = target.read_bytes()
    assert consume_restart_marker(paths, old_pid_alive=lambda _: False, consume=False) == marker
    assert target.read_bytes() == before
    assert consume_restart_marker(paths, old_pid_alive=lambda _: False) == marker
    assert not target.exists()


def test_fresh_planned_marker_is_preserved_when_b4_not_supported(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, control, _gate, _ctx):
        context = context_for(original, control, tmp_path)
        target = restart_completed_path(context.paths)
        write_json_file_atomic(target, {"schema": COMPLETED_SCHEMA, "old_pid": 999999999, "drained_at": time.time()})
        before = target.read_bytes(), original.claim.path.read_bytes()
        with pytest.raises(RuntimeError, match="GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED"):
            coordinated.recover_coordinated_startup(context)
        assert (target.read_bytes(), original.claim.path.read_bytes()) == before
        assert context.drain_runtime._admission_origins == []


def test_closed_real_root_cannot_start_recovery_or_consume_marker(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, control, _gate, _ctx):
        context = context_for(original, control, tmp_path)
        context.drain_runtime.stop_producers()
        before = original.claim.path.read_bytes()
        with pytest.raises(ValueError):
            coordinated.recover_coordinated_startup(context)
        assert original.claim.path.read_bytes() == before


def test_startup_preserves_current_live_pid_even_when_lease_is_old(tmp_path, monkeypatch):
    with cold_case(tmp_path, monkeypatch) as (original, control, _gate, _ctx):
        context = context_for(original, control, tmp_path)
        payload = read_json_file(original.claim.path)
        assert payload["lease_process_identity"]["pid"] == os.getpid()
        before = original.claim.path.read_bytes()
        report = coordinated.recover_coordinated_startup(context)
        assert report.summary["checked"] == 1 and report.summary["requeued"] == 0
        assert report.summary["failed"] == 0 and not report.load_errors
        assert original.claim.path.read_bytes() == before
