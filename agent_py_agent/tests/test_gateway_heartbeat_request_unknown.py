"""原Curator心跳出处与真实C/W claim；只故障注入Thread启动ACK/join，不制造执行权。"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from contextvars import Context, copy_context

import pytest

from agent_py_agent.agent.gateway_parts import request_errors, request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.memory_store import curator_lease
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.tests.test_gateway_claim_transition_scope import actual as actual
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_claim_transition_scope import context as context
from agent_py_agent.tests.test_gateway_request_execution_unknown import _second_actual_claim


# LLM: 原heartbeat/Thread/renew都保留；仅令原join或启动ACK抛可控异常，原join最终清理，不签发执行身份。
# 函数用途: 在调用者原scope内故障注入心跳设施，返回原异常与真实出生对象。
@contextmanager
def _heartbeat_fault(monkeypatch, *, start_error=False):
    real_constructor = curator_lease._LeaseHeartbeat.__init__
    real_start, real_join, real_renew = threading.Thread.start, threading.Thread.join, curator_lease.renew_curator_lease
    entered, release = threading.Event(), threading.Event()
    born = []
    original = RuntimeError("synthetic-start-ack-error" if start_error else "synthetic-join-unknown")

    def constructor(heartbeat, *args):
        real_constructor(heartbeat, *args)
        born.append(heartbeat)

    def renew(*args):
        if born and threading.current_thread() is born[0].thread:
            entered.set()
            assert release.wait(5)
        return real_renew(*args)

    def start(thread):
        real_start(thread)
        if born and thread is born[0].thread and start_error:
            assert entered.wait(3)
            raise original

    def join(thread, *args, **kwargs):
        if born and thread is born[0].thread:
            assert entered.wait(3)
            raise original
        return real_join(thread, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(curator_lease._LeaseHeartbeat, "__init__", constructor)
        patch.setattr(curator_lease, "renew_curator_lease", renew)
        patch.setattr(threading.Thread, "start", start)
        patch.setattr(threading.Thread, "join", join)
        try:
            yield original, born, entered
        finally:
            release.set()
            for heartbeat in born:
                heartbeat.stop.set()
                real_join(heartbeat.thread, 3)
                assert not heartbeat.thread.is_alive()


# LLM: 租约由真实state acquire创建并在原C/W短门内运行；不从测试数据伪造lease或source。
# 函数用途: 保持真实线程仍活，供同claim及失效来源负例检查。
@contextmanager
def _held_heartbeat(agent, monkeypatch, *, start_error=False):
    store = MemoryCuratorStateStore(agent.home_paths.owner_home_dir / "memory/curator/state.json")
    _state, lease = store.acquire(reason="admin", config_revision="synthetic", lease_seconds=30)
    lease_id = lease["lease_id"]
    with _heartbeat_fault(monkeypatch, start_error=start_error) as (original, born, entered):
        with pytest.raises(RuntimeError) as caught:
            with curator_lease.curator_lease_heartbeat(store, lease_id, 0):
                assert entered.wait(3)
        assert caught.value is original and born[0].thread.is_alive()
        assert store.load().active_lease["phase"] == "executing"
        yield original, lease_id, born[0], store


@pytest.mark.parametrize("start_error", [False, True])
def test_actual_background_entry_preserves_heartbeat_unknown_before_scope_exit(tmp_path, monkeypatch, start_error):
    from agent_py_agent.agent.memory_store import curator as curator_module
    from agent_py_agent.tests.test_gateway_background_curator_transactions import (
        curator_case,
        run_original,
    )

    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _close):
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        monkeypatch.setattr(curator_module, "_lease_seconds", lambda _config: 0)
        # 仅隔离心跳设施；正常join-unknown用空业务主体，ACK-unknown则主体根本不进入。
        with _heartbeat_fault(monkeypatch, start_error=start_error) as (error, born, _entered):
            monkeypatch.setattr(service, "_execute", lambda _context: _entered.wait(3) or pytest.fail("original heartbeat must actually enter"))
            caught, response = None, None
            try:
                response = run_original(agent, gate, claim)
            except RuntimeError as observed:
                caught = observed
            pending = read_json_file(claim.path)
            assert caught is error, {"response_ok": None if response is None else response.get("ok"),
                "request_status": pending.get("status"), "has_execution_outcome": "execution_outcome" in pending,
                "heartbeat_alive": born[0].thread.is_alive(),
                "lease_phase": service.state_store.load().active_lease.get("phase"),
                "model_calls": backend.calls}
            assert backend.calls == 0 and born[0].thread.is_alive()
            lease = service.state_store.load().active_lease
            assert lease["phase"] == "executing"
            pending = read_json_file(claim.path)
            assert pending["status"] == "processing" and pending["execution_outcome"]["lease_id"] == lease["lease_id"]
            assert pending["execution_outcome"]["operation_entered"] is None
            assert pending["execution_outcome"]["error_code"] == "TASK_RESOURCE_STOP_UNCONFIRMED"
            assert service.candidate_service.list() == [] and service.run_log.list() == []
            request_worker.terminalize_unhandled_claimed_gateway_request(
                gate.paths, claim.path, error, agent=agent,
                expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
            )
            assert read_json_file(claim.path) == pending and p3.read_bytes() == before
            assert not (gate.paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize("role", ["C", "W"])
@pytest.mark.parametrize("start_error", [False, True])
def test_original_heartbeat_unknown_records_same_claim_without_fake_call_identity(request, monkeypatch, role, start_error):
    if role == "C":
        gate, claim, agent = request.getfixturevalue("c_actual")
        paths, scope = gate.paths, coordinator_execution_scope(gate, claim, agent)
    else:
        binding, _payload, agent = request.getfixturevalue("actual")
        claim = binding.claim
        paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
        scope = worker_execution_scope(binding, agent)
    before = read_json_file(claim.path)
    p3 = reservation_file(paths, claim.request_id)
    original_p3 = p3.read_bytes()
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    with scope, request_transaction_ports_scope(agent):
        with _held_heartbeat(agent, monkeypatch, start_error=start_error) as (error, lease_id, heartbeat, store):
            origin = curator_lease.lease_heartbeat_error_origin(error)
            assert origin is not None and origin[1] == lease_id
            assert request_errors.gateway_request_execution_unknown(error, before, expected)
            request_worker.terminalize_unhandled_claimed_gateway_request(
                paths, claim.path, error, agent=agent,
                expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
            )
            current = read_json_file(claim.path)
            observation = current.pop("execution_outcome")
            assert current == before and p3.read_bytes() == original_p3
            assert observation == {"schema_version": "gateway_execution_outcome.v1", "status": "unknown",
                "request_id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner,
                "error_code": "TASK_RESOURCE_STOP_UNCONFIRMED", "phase": "curator_lease_heartbeat_stop",
                "operation_entered": None, "lease_id": lease_id}
            assert "call_id" not in observation and not hasattr(error, "operation_entered")
            assert heartbeat.thread.is_alive() and store.load().active_lease["lease_id"] == lease_id
    original = claim.path.read_bytes()
    request_worker.terminalize_unhandled_claimed_gateway_request(
        paths, claim.path, error, agent=agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    assert claim.path.read_bytes() == original and p3.read_bytes() == original_p3
    assert not (paths.terminal / claim.path.name).exists() and not (paths.responses / claim.path.name).exists()


@pytest.mark.parametrize("fault", ["copy", "marker", "payload_id", "attempt", "epoch", "owner", "expected", "new_scope", "empty_context", "second_claim"])
def test_heartbeat_origin_cannot_authorize_copied_foreign_or_replaced_claim(c_actual, monkeypatch, fault):
    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    second = _second_actual_claim(gate) if fault == "second_claim" else None
    before = claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        with _held_heartbeat(agent, monkeypatch) as (original, _lease_id, _heartbeat, _store):
            error = original
            if fault == "copy":
                error = copy.copy(original)
            elif fault == "marker":
                error = RuntimeError("marker-only")
                curator_lease.lease_exception_metadata(error)["lease_heartbeat_stop_error_class"] = "RuntimeError"
            elif fault in {"payload_id", "attempt", "epoch", "owner"}:
                key = {"payload_id": "id", "attempt": "execution_attempt_id", "epoch": "lease_epoch", "owner": "lease_owner"}[fault]
                payload[key] = payload[key] + 1 if fault == "epoch" else "another"
            elif fault == "expected":
                expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch + 1)
            def check():
                return request_errors.gateway_request_execution_unknown(error, payload, expected)
            if fault == "new_scope":
                with coordinator_execution_scope(gate, claim, agent):
                    assert check() is False
            elif fault == "empty_context":
                assert Context().run(check) is False
            elif fault == "second_claim":
                with coordinator_execution_scope(gate, second, agent):
                    assert request_errors.gateway_request_execution_unknown(error, read_json_file(second.path),
                        (second.request_id, second.execution_attempt_id, second.lease_epoch)) is False
            else:
                assert check() is False
    assert (claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()) == before


def test_expired_original_heartbeat_source_preserves_unresolved_claim_without_new_observation(c_actual, monkeypatch):
    gate, claim, agent = c_actual
    payload = read_json_file(claim.path)
    expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
    before = claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
    with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
        with _held_heartbeat(agent, monkeypatch) as (error, _lease_id, _heartbeat, _store):
            captured = copy_context()
    assert captured.run(request_errors.gateway_request_execution_unknown, error, payload, expected) is False
    request_worker.terminalize_unhandled_claimed_gateway_request(
        gate.paths, claim.path, error, agent=agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    assert (claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()) == before
    assert not (gate.paths.terminal / claim.path.name).exists()
