"""真实C临时租约/P3准入派发；不启动实际C/W、模型或业务线程。"""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    coordinator_admission_gate,
    reservation_file,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _prepare, _request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 假业务对象仅提供typed home/config；实际C/control的唯一Root登记同gate，lease/receipt/P3/claim仍由原入口创建，不伪造binding。
# 函数用途: 为同次临时C建立实际准入门，并清空独立的原Count0内存计数用于对照。
def _gate(context, monkeypatch):
    agent = SimpleNamespace(config=context.config, home_paths=home_paths(context.home_root))
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    gate = coordinator_admission_gate(agent, context.paths, SimpleNamespace(coordinator_worker_count=2))
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    runtime = getattr(context, "drain_runtime", None) or GatewayDrainRuntime(context)
    runtime.retain_admission(gate)
    return gate, agent


def _enqueue(context, *, local=False):
    payload = _request()
    if local:
        payload["metadata"].update(channel="chat", user_id="local-agent")
        payload["user_id"] = "local-agent"
        from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint
        _fingerprint(payload)
    receipt, _ = _prepare(context.paths, payload)
    with delivery.gateway_input_transition(context.paths, receipt.request_id):
        receipt, _ = delivery.queue_gateway_input_locked(context.paths, receipt)
    return receipt


@pytest.mark.parametrize("local", [True, False])
def test_actual_gate_routes_c_claim_or_w_pending_without_count0_slots(context, monkeypatch, local):
    gate, _agent = _gate(context, monkeypatch)
    receipt = _enqueue(context, local=local)
    original = copy.deepcopy(receipt.prepared_request)
    submitted = []
    claimed = worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(8, 8),
        lambda claim, user, conversation: submitted.append((claim, user, conversation)), admission_gate=gate)
    assert claimed == int(local) and len(submitted) == int(local)
    reservation = read_json_file(reservation_file(context.paths, receipt.request_id))
    assert reservation["state"] == "occupied" and gate.occupied_snapshot()["total"] == 1
    assert worker.admission.snapshot()["total"] == 0
    path = (context.paths.processing if local else context.paths.inbox) / (receipt.request_id + ".json")
    stamped = read_json_file(path)
    assert stamped["metadata"] == original["metadata"]
    assert stamped["input_media"] == original["input_media"]
    assert stamped["request_fingerprint"] == original["request_fingerprint"]
    assert frozen_request_owner(stamped) == frozen_request_owner(original)
    assert stamped["route"]["worker"] == ("C" if local else reservation["route"]["worker"])
    assert delivery.read_gateway_input_receipt(context.paths, receipt.request_id) == receipt
    if local:
        assert submitted[0][0].coordinator_snapshot.gate is gate


def test_actual_gate_submit_failure_retains_original_p3_and_claim(context, monkeypatch):
    gate, _agent = _gate(context, monkeypatch)
    receipt = _enqueue(context, local=True)
    def failed(_claim, _user, _conversation):
        raise RuntimeError("synthetic_submit_failure")
    with pytest.raises(RuntimeError, match="synthetic_submit_failure"):
        worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(8, 8), failed, admission_gate=gate)
    assert gate.occupied_snapshot()["total"] == 1
    assert (context.paths.processing / (receipt.request_id + ".json")).exists()
    assert not (context.paths.terminal / (receipt.request_id + ".json")).exists()
    assert worker.admission.snapshot()["total"] == 0


def test_actual_gate_rejects_changed_owner_before_p3_and_keeps_original_receipt(context, monkeypatch):
    from agent_py_agent.tests.test_gateway_prepared_owner_identity import _changed_owner
    gate, _agent = _gate(context, monkeypatch)
    receipt = _enqueue(context)
    changed, _other = _changed_owner(context, receipt.prepared_request)
    path = context.paths.inbox / (receipt.request_id + ".json")
    write_json_file_atomic(path, changed)
    called = []
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(8, 8),
        lambda *_: called.append(True), admission_gate=gate) == 0
    assert not called and not reservation_file(context.paths, receipt.request_id).exists()
    assert read_json_file(path)["admission_wait_reason"] == "prepared_input_invalid"
    assert delivery.read_gateway_input_receipt(context.paths, receipt.request_id) == receipt


def test_context_dto_exposes_same_cold_configuration_without_creating_agent(context):
    current = GatewayRunContext(None, context.paths, Path("synthetic.yaml"),
        coordinator_worker_count=2, control_plane=context)
    assert current.config is context.config and current.agent is None
    assert current.drain_runtime is None


def test_dispatcher_builds_original_admission_gate_from_actual_c_context(context, monkeypatch):
    from agent_py_agent.cli import gateway_loops
    gate, agent = _gate(context, monkeypatch)
    # 测试不提交任务；假executor仅记录原pool构造参数，不替代任何来源或权限检查。
    class Executor:
        def __init__(self, **kwargs):
            self.arguments = kwargs
        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(gateway_loops, "DurableDaemonThreadPoolExecutor", Executor)
    current = GatewayRunContext(agent, context.paths, Path("synthetic.yaml"),
        coordinator_worker_count=2, control_plane=context)
    dispatcher = gateway_loops._RequestDispatcher(current, context.paths)
    assert dispatcher._admission_gate is not gate
    assert dispatcher._admission_gate._runtime_takeover is context.controller()
    assert dispatcher._admission_gate.agent is agent
    assert dispatcher._admission_gate.c_run_id == context.c_run_id
    assert dispatcher._executor.arguments["max_workers"] == dispatcher.limits.global_inflight
    dispatcher.shutdown()


def test_original_c_closing_does_not_refresh_pending_wait_or_create_p3(context, monkeypatch):
    gate, _agent = _gate(context, monkeypatch)
    receipt = _enqueue(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    before = path.read_bytes()
    # 与原撤权入口相同的生命周期门/closing事实；不造新的lease或伪装成已退出。
    lease = context._coordinator_role_lease
    with lease._lifecycle_gate:
        lease._closing = True
    called = []
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(8, 8),
        lambda *_: called.append(True), admission_gate=gate) == 0
    assert not called and path.read_bytes() == before
    assert not reservation_file(context.paths, receipt.request_id).exists()


def test_wait_cas_cannot_write_after_top_level_owner_changed(context, monkeypatch):
    from agent_py_agent.tests.test_gateway_prepared_owner_identity import _changed_owner
    gate, _agent = _gate(context, monkeypatch)
    receipt = _enqueue(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    changed, _ = _changed_owner(context, receipt.prepared_request)
    write_json_file_atomic(path, changed)
    before = path.read_bytes()
    assert worker._write_admission_wait_field(path,
        {"admission_wait_at": 10.0, "admission_wait_reason": "synthetic"},
        expected_payload=receipt.prepared_request, paths=context.paths, admission_gate=gate) is False
    assert path.read_bytes() == before
