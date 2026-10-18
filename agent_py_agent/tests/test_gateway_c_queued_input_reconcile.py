"""真实C/Root与原receipt的queue/cursor修复；未结输入不构造Agent或读Store。"""

import copy
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module

import pytest

from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import CoordinatorUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.runtime_errors import RecoverableRuntimeError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
from agent_py_agent.tests.test_gateway_cold_request_pump import pump, queue_request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 只在新测试调用时导入组件，原生产前像先留下真实tick/旧caller红，不用collection缺模块替代因果。
# 函数用途: 返回本片C元数据消费者，不构造权限替身。
def module():
    return import_module("agent_py_agent.agent.gateway_parts.multiprocess_input_reconcile")


# LLM: 用原pump构造取得已被Root保留的原gate，不造新的P3或Agent；每次直接scan仍使用该同一context。
# 函数用途: 在真实C内调用同一个queued/cursor修复入口。
def scan(current, *, limit=64):
    return module().reconcile_coordinator_queued_inputs(
        current.context, current.paths, current._admission_gate, limit=limit
    )


# LLM: 这里只种一条待源对账的合成旧receipt，不声称guidance存在/消费，更不签当前claim。
# 函数用途: 为不支持状态保留正文/完整prepared，验证C不会擅自加载业务Store。
def defer_receipt(control, identifier="a-deferred", state="active_pending"):
    receipt = queue_request(control, identifier)
    (control.paths.inbox / (identifier + ".json")).unlink()
    receipt = replace(receipt, state=state, target_turn_id="original-target")
    write_json_file_atomic(
        inputs.gateway_input_receipt_path(control.paths, identifier), receipt.to_dict()
    )
    return receipt


# LLM: 当前宿主是原真实SimpleAgent，C/control/Root/gate均沿原fixture和生产dispatcher；不补假Agent属性。
# 函数用途: 准备healthy C原调用点，finally只关闭未启动线程的原池。
@contextmanager
def healthy(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _service, _backend, gate, _claim, _close):
        control = gate._runtime_takeover._lease.control_plane
        agent._gateway_control_plane = control
        context = GatewayRunContext(
            agent,
            gate.paths,
            tmp_path / "config.yaml",
            coordinator_worker_count=control.worker_count,
            control_plane=control,
            drain_runtime=GatewayDrainRuntime(control),
        )
        current = loops._RequestDispatcher(context, gate.paths)
        try:
            yield current, control
        finally:
            current.shutdown()


def test_actual_cold_tick_repairs_original_queued_receipt_without_business(context, monkeypatch):
    receipt = queue_request(context, "queued-original")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    before = inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes()
    monkeypatch.setattr(
        request_worker, "_resolve_request_agent", lambda *_: pytest.fail("foreign Agent")
    )
    current = pump(context)
    current.tick()
    assert read_json_file(target) == receipt.prepared_request
    assert (
        inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes() == before
    )
    assert not reservation_file(context.paths, receipt.request_id).exists()
    assert not resolve_owner_home(
        context.home_root, OwnerIdentity.provider_user("feishu", "cold-alice")
    ).home_dir.exists()


def test_actual_healthy_c_refuses_queue_and_cursor_after_root_closed(tmp_path, monkeypatch):
    with healthy(tmp_path, monkeypatch) as (current, control):
        receipt = queue_request(control, "healthy-closed")
        target = control.paths.inbox / (receipt.request_id + ".json")
        target.unlink()
        current.context.drain_runtime.stop_producers()
        with pytest.raises(RequestExecutionContextUnavailable):
            current._reconcile_inputs_once()
        assert not target.exists()
        assert not (control.paths.root / "input_receipts/state/reconcile_cursor.json").exists()


def test_actual_healthy_c_defers_active_without_foreign_agent(tmp_path, monkeypatch):
    with healthy(tmp_path, monkeypatch) as (current, control):
        receipt = defer_receipt(control)
        path = inputs.gateway_input_receipt_path(control.paths, receipt.request_id)
        before, calls = path.read_bytes(), []

        def forbidden(*_args):
            calls.append(True)
            raise AssertionError("foreign Agent/Store must not be read")

        monkeypatch.setattr(request_worker, "_resolve_request_agent", forbidden)
        with pytest.raises(RecoverableRuntimeError):
            current._reconcile_inputs_once()
        assert calls == [] and path.read_bytes() == before


def test_quarantined_queued_repair_does_not_require_new_p3_or_owner_business(context):
    receipt = queue_request(context, "quarantined-queue")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    owner = OwnerIdentity.provider_user("feishu", "cold-alice")
    context.controller()._unproven_owners.add(owner_digest(owner))
    result = scan(pump(context))
    assert result["queued"] == 1 and result["errors"] == result["deferred"] == 0
    assert read_json_file(target) == receipt.prepared_request
    assert not reservation_file(context.paths, receipt.request_id).exists()


@pytest.mark.parametrize("state", ["pending", "active_pending", "terminal_unknown"])
def test_deferred_prefix_rotates_to_later_queued_without_store(context, monkeypatch, state):
    receipt = defer_receipt(context, state=state)
    path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
    before = path.read_bytes()
    queued = queue_request(context, "z-queued")
    target = context.paths.inbox / (queued.request_id + ".json")
    target.unlink()
    monkeypatch.setattr(
        inputs, "gateway_input_guidance_binding", lambda *_: pytest.fail("guidance read/migration")
    )
    current = pump(context)
    first = scan(current, limit=1)
    second = scan(current, limit=1)
    assert first["deferred"] == 1 and first["queued"] == first["consumed"] == 0
    assert first["unsupported_states"] == {state: 1}
    assert second["queued"] == 1 and second["deferred"] == 0
    assert path.read_bytes() == before and read_json_file(target) == queued.prepared_request


def test_all_deferred_cold_segment_reports_and_backs_off(context, monkeypatch):
    receipt = defer_receipt(context)
    path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
    before = path.read_bytes()
    current = pump(context)
    current.tick()
    assert current._last_input_report["deferred"] == 1
    assert current._last_input_report["queued"] == current._last_input_report["consumed"] == 0
    assert current._reconcile_backoff.consecutive_failures == 1
    assert current._next_input_reconcile_at > 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault", ["copy", "runtime", "gate", "paths", "closed", "pid", "thread"])
def test_original_source_rejects_reference_or_lifetime_drift(context, fault):
    current = pump(context)
    source = module()._QueuedInputSource(current.context, current.paths, current._admission_gate)
    if fault == "copy":
        source = copy.copy(source)
    elif fault == "runtime":
        source.runtime = copy.copy(source.runtime)
    elif fault == "gate":
        source.gate = copy.copy(source.gate)
    elif fault == "paths":
        source.paths = copy.copy(source.paths)
    elif fault == "closed":
        source.runtime.stop_producers()
    elif fault == "pid":
        source._pid += 1
    else:
        source._thread += 1
    with pytest.raises(RequestExecutionContextUnavailable):
        source.require_original()


@pytest.mark.parametrize("where", ["queue", "cursor"])
def test_last_writer_refusal_prevents_late_write_and_cursor(context, monkeypatch, where):
    receipt = queue_request(context, "last-writer")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    current = pump(context)
    original = inputs.write_json_file_atomic

    def revoke_before_call(path, value):
        pytest.fail("refused source reached atomic writer")

    if where == "queue":
        materialize = inputs._materialize_prepared_request_locked

        def late(paths, row, **kwargs):
            current.runtime.stop_producers()
            return materialize(paths, row, **kwargs)

        monkeypatch.setattr(inputs, "_materialize_prepared_request_locked", late)
    else:
        commit = inputs._commit_reconcile_cursor

        def late(paths, processed_path, **kwargs):
            current.runtime.stop_producers()
            return commit(paths, processed_path, **kwargs)

        monkeypatch.setattr(inputs, "_commit_reconcile_cursor", late)
    with pytest.raises(RequestExecutionContextUnavailable):
        scan(current)
    assert not (context.paths.root / "input_receipts/state/reconcile_cursor.json").exists()
    if where == "queue":
        assert not target.exists()
    else:
        assert read_json_file(target) == receipt.prepared_request
    assert original is inputs.write_json_file_atomic


@pytest.mark.parametrize("kind", ["transport", "context", "coordinator", "bounded", "global"])
@pytest.mark.parametrize("where", ["queue", "cursor"])
def test_actual_typed_middle_row_preserves_original_exception_and_cursor(
    context, monkeypatch, kind, where
):
    current = pump(context)
    for name in ("a-first", "b-failed", "c-last"):
        receipt = queue_request(context, name)
        (context.paths.inbox / (receipt.request_id + ".json")).unlink()
    if kind == "transport":
        error = RequestExecutionOutcomeUncertainError(
            {
                "id": "original-fault",
                "execution_attempt_id": "original-attempt",
                "lease_epoch": 1,
                "lease_owner": "original-attempt",
            },
            "synthetic-original-read",
        )
    elif kind == "context":
        error = RequestExecutionContextUnavailable("original synthetic source")
    elif kind == "coordinator":
        error = CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    elif kind == "global":
        error = GlobalWriteInWorkerError("gateway_queued_input")
    else:
        from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

        error = BoundedCallExitUnconfirmedError(
            "synthetic-original-call", "worker_exit_unconfirmed"
        )
    if where == "queue":
        original = inputs._materialize_prepared_request_locked

        def fail(paths, row, **kwargs):
            if row.request_id == "b-failed":
                raise error
            return original(paths, row, **kwargs)

        monkeypatch.setattr(inputs, "_materialize_prepared_request_locked", fail)
    else:
        original = inputs._commit_reconcile_cursor

        def fail(paths, path, **kwargs):
            if path.stem == "b-failed":
                raise error
            return original(paths, path, **kwargs)

        monkeypatch.setattr(inputs, "_commit_reconcile_cursor", fail)
    with pytest.raises(type(error)) as caught:
        scan(current)
    assert caught.value is error
    cursor = read_json_file(context.paths.root / "input_receipts/state/reconcile_cursor.json")
    assert cursor["last_name"] == "a-first.json"
    assert not (context.paths.inbox / "c-last.json").exists()


def test_same_code_ordinary_failure_rotates_and_later_queue_is_repaired(context, monkeypatch):
    current = pump(context)
    for name in ("a-bad", "z-good"):
        receipt = queue_request(context, name)
        (context.paths.inbox / (receipt.request_id + ".json")).unlink()
    original = inputs._materialize_prepared_request_locked
    error = RuntimeError("synthetic ordinary")
    error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"

    def fail(paths, row, **kwargs):
        if row.request_id == "a-bad":
            raise error
        return original(paths, row, **kwargs)

    monkeypatch.setattr(inputs, "_materialize_prepared_request_locked", fail)
    result = scan(current)
    assert result["errors"] == 1 and result["queued"] == 1
    assert (
        read_json_file(context.paths.root / "input_receipts/state/reconcile_cursor.json")[
            "last_name"
        ]
        == "z-good.json"
    )
    assert (context.paths.inbox / "z-good.json").exists()


def test_same_receipt_prepared_owner_actor_media_and_digest_are_not_reprepared(
    context, monkeypatch
):
    receipt = queue_request(context, "immutable")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    before = inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes()
    monkeypatch.setattr(
        type(context), "prepare_input", lambda *_args, **_kwargs: pytest.fail("reprepare")
    )
    result = scan(pump(context))
    assert result["queued"] == 1 and read_json_file(target) == receipt.prepared_request
    assert (
        inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes() == before
    )


def test_receipt_change_inside_leaf_is_rejected_before_atomic_queue_write(context, monkeypatch):
    receipt = queue_request(context, "receipt-cas")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    original = inputs._materialize_prepared_request_locked

    def replace_receipt(paths, row, **kwargs):
        path = inputs.gateway_input_receipt_path(paths, row.request_id)
        write_json_file_atomic(path, {**row.to_dict(), "state": "consumed"})
        return original(paths, row, **kwargs)

    monkeypatch.setattr(inputs, "_materialize_prepared_request_locked", replace_receipt)
    with pytest.raises(RequestExecutionContextUnavailable):
        scan(pump(context))
    assert not target.exists()
    assert not (context.paths.root / "input_receipts/state/reconcile_cursor.json").exists()


def test_actual_healthy_c_repairs_queue_without_foreign_agent(tmp_path, monkeypatch):
    with healthy(tmp_path, monkeypatch) as (current, control):
        receipt = queue_request(control, "healthy-original")
        target = control.paths.inbox / (receipt.request_id + ".json")
        target.unlink()
        monkeypatch.setattr(
            request_worker, "_resolve_request_agent", lambda *_: pytest.fail("foreign Agent")
        )
        current._reconcile_inputs_once()
        assert read_json_file(target) == receipt.prepared_request
        assert not reservation_file(control.paths, receipt.request_id).exists()


def test_revoke_during_final_receipt_read_never_reaches_queue_writer(context, monkeypatch):
    receipt = queue_request(context, "final-read")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    current = pump(context)
    source_type = module()._QueuedInputSource
    original = source_type._require_queue_writer
    reader = inputs.read_gateway_input_receipt

    def read_then_revoke(*args, **kwargs):
        row = reader(*args, **kwargs)
        current.runtime.stop_producers()
        return row

    def late(source, window):
        with monkeypatch.context() as change:
            change.setattr(inputs, "read_gateway_input_receipt", read_then_revoke)
            return original(source, window)

    monkeypatch.setattr(source_type, "_require_queue_writer", late)
    with pytest.raises(RequestExecutionContextUnavailable):
        scan(current)
    assert not target.exists()
    assert not (context.paths.root / "input_receipts/state/reconcile_cursor.json").exists()


@pytest.mark.parametrize("drift", ["owner", "actor", "media"])
def test_existing_foreign_or_changed_queue_is_never_overwritten(context, drift):
    receipt = queue_request(context, "existing-drift")
    target = context.paths.inbox / (receipt.request_id + ".json")
    value = read_json_file(target)
    if drift == "owner":
        value["owner_id"] = "providers/feishu/users/other"
    elif drift == "actor":
        value["metadata"]["user_id"] = "other-actor"
    else:
        value["input_media"] = [{"kind": "image", "ref": "other-media"}]
    write_json_file_atomic(target, value)
    before = target.read_bytes()
    result = scan(pump(context))
    assert result["errors"] == 1 and result["queued"] == 0
    assert target.read_bytes() == before


def test_orphan_projection_does_not_authorize_queue_replay(context):
    receipt = queue_request(context, "orphan-projection")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    projection = context.paths.done / (receipt.request_id + ".json")
    write_json_file_atomic(projection, receipt.prepared_request)
    before = projection.read_bytes()
    result = scan(pump(context))
    assert result["errors"] == 1 and result["queued"] == 0
    assert projection.read_bytes() == before and not target.exists()


@pytest.mark.parametrize("where", ["queue", "cursor"])
def test_retained_atomic_callback_cannot_outlive_original_scan(context, monkeypatch, where):
    receipt = queue_request(context, "escaped-writer")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    captured = []
    helper = (
        "_materialize_prepared_request_locked" if where == "queue" else "_commit_reconcile_cursor"
    )
    keyword = "write_request" if where == "queue" else "write_cursor"
    original = getattr(inputs, helper)

    def capture(*args, **kwargs):
        captured.append(kwargs[keyword])
        return original(*args, **kwargs)

    monkeypatch.setattr(inputs, helper, capture)
    scan(pump(context))
    if where == "queue":
        target.unlink()
        path, payload = target, receipt.prepared_request
        before = None
    else:
        path = context.paths.root / "input_receipts/state/reconcile_cursor.json"
        payload, before = read_json_file(path), path.read_bytes()
    with pytest.raises(RequestExecutionContextUnavailable):
        captured[0](path, payload)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("missing", ["gate", "control", "runtime"])
def test_healthy_c_missing_source_never_falls_back_to_count0(tmp_path, monkeypatch, missing):
    with healthy(tmp_path, monkeypatch) as (current, control):
        receipt = queue_request(control, "missing-source")
        target = control.paths.inbox / (receipt.request_id + ".json")
        target.unlink()
        if missing == "gate":
            current._admission_gate = None
        else:
            key = "control_plane" if missing == "control" else "drain_runtime"
            current.context = replace(current.context, **{key: None})
        monkeypatch.setattr(
            request_worker, "_resolve_request_agent", lambda *_: pytest.fail("foreign Agent")
        )
        with pytest.raises(RequestExecutionContextUnavailable):
            current._reconcile_inputs_once()
        assert not target.exists()


def test_original_global_restriction_refuses_c_queue_source_before_writes(context, monkeypatch):
    from agent_py_agent.agent.common import global_write_guard

    receipt = queue_request(context, "restricted-source")
    target = context.paths.inbox / (receipt.request_id + ".json")
    target.unlink()
    current = pump(context)
    monkeypatch.setattr(global_write_guard, "_restricted_writer", lambda: object())
    with pytest.raises(GlobalWriteInWorkerError):
        scan(current)
    assert not target.exists()
    assert not (context.paths.root / "input_receipts/state/reconcile_cursor.json").exists()


@pytest.mark.parametrize("state", ["pending", "active_pending", "terminal_unknown"])
def test_queued_source_cannot_be_reused_to_materialize_bound_or_unknown_input(context, state):
    receipt = defer_receipt(context, state=state)
    current = pump(context)
    source = module()._QueuedInputSource(current.context, current.paths, current._admission_gate)
    path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
    before = path.read_bytes()
    with pytest.raises(RequestExecutionContextUnavailable):
        source.repair_queued(receipt)
    assert path.read_bytes() == before
    assert not (context.paths.inbox / (receipt.request_id + ".json")).exists()
