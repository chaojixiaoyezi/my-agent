"""W4：历史悬空链接分级与 steer/stop 的确定性生命周期。"""
import pytest

from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_execution import _require_gateway_conversation_ready
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_unpromoted_stop import hot as hot


def _missing_history(hot):
    store = hot.agent.conversation_store
    for task_id in ("missing-one", "missing-two"):
        store.tasks.bind({"task_id": task_id, "thread_id": hot.scope.thread_id, "status": "completed"})
        store.storage.task_path(task_id).unlink()
    return store


def test_missing_historical_links_do_not_block_current_context(hot):
    store = _missing_history(hot)
    _links, errors = store.tasks.list_report(hot.scope.thread_id)
    assert len(errors) == 2
    context = gateway_conversation_context(GatewayConversationLoadRequest(
        hot.agent, hot.payload, "message", "继续", defer_compact=True))
    assert not context.load_errors
    assert {row["id"] for row in context.load_warnings} == {"missing-one", "missing-two"}
    assert all(row["code"] == "missing_historical_task_link" for row in context.load_warnings)
    assert set(store.threads.require(hot.scope.thread_id).task_ids) == {"missing-one", "missing-two"}
    _require_gateway_conversation_ready(hot.payload, context)


def test_corrupt_historical_link_degrades_to_warning(hot):
    store = _missing_history(hot)
    store.tasks.bind({"task_id": "corrupt", "thread_id": hot.scope.thread_id, "status": "completed"})
    store.storage.task_path("corrupt").write_text("{broken", encoding="utf-8")
    context = gateway_conversation_context(GatewayConversationLoadRequest(
        hot.agent, hot.payload, "message", "继续", defer_compact=True))
    assert not context.load_errors
    assert any(row.get("task_id") == "corrupt" for row in context.load_warnings)
    _require_gateway_conversation_ready(hot.payload, context)


def _promote_same_request_without_backfill(hot):
    store = hot.agent.conversation_store
    record = hot.repo.record_run_creation(
        owner_id=hot.agent.home_paths.owner_id, conversation_task_id="message",
        run_id="promoted-main", thread_id=hot.scope.thread_id, role="main",
    )
    hot.writer.bind_runtime_authority({
        "invocation_run_id": "message", "task_id": "message", "run_id": "promoted-main",
        "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"],
    })
    store.tasks.bind({"task_id": "message", "thread_id": hot.scope.thread_id, "status": "active"})
    return record


def test_stop_promoted_request_without_runtime_task_backfill(hot):
    from agent_py_agent.agent.gateway_parts import control_service

    record = _promote_same_request_without_backfill(hot)
    assert "message" in hot.agent.conversation_store.threads.require(hot.scope.thread_id).active_task_ids
    result = control_service._stop_live_window_request(
        hot.agent, hot.paths, control_service._GatewayRequestRecord(hot.path, hot.payload), hot.control_scope,
    )
    assert result.ok, (result.error_code, result.message)
    assert result.error_code != "TASK_RESOURCE_STOP_UNCONFIRMED"
    assert "message" not in hot.agent.conversation_store.threads.require(hot.scope.thread_id).active_task_ids
    assert hot.repo.get_attempt(record["attempt_id"])["status"] == "cancelled"


def test_stop_repairs_cancelled_promoted_task_active_index(hot):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts import control_service

    _promote_same_request_without_backfill(hot)
    store = hot.agent.conversation_store
    store.tasks.update_status({"task_id": "message", "status": "cancelled"})
    store.threads.update_atomic(hot.scope.thread_id, lambda thread: replace(thread, active_task_ids=("message",)))
    result = control_service._stop_live_window_request(
        hot.agent, hot.paths, control_service._GatewayRequestRecord(hot.path, hot.payload), hot.control_scope,
    )
    assert result.ok, (result.error_code, result.message)
    assert "message" not in store.threads.require(hot.scope.thread_id).active_task_ids


def test_stop_same_id_promoted_task_ignores_only_missing_history(hot):
    from agent_py_agent.agent.gateway_parts import control_service

    _missing_history(hot)
    record = _promote_same_request_without_backfill(hot)
    result = control_service._stop_live_window_request(
        hot.agent, hot.paths, control_service._GatewayRequestRecord(hot.path, hot.payload), hot.control_scope,
    )
    assert result.ok, (result.error_code, result.message)
    assert "message" not in hot.agent.conversation_store.threads.require(hot.scope.thread_id).active_task_ids
    assert hot.repo.get_attempt(record["attempt_id"])["status"] == "cancelled"


def _terminal_target(paths, status="cancelled", *, source=None):
    import copy

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file

    ensure_gateway_folders(paths)
    path = paths.processing / "target.json"
    payload = copy.deepcopy(source) if source is not None else _count0_steer()
    payload.update(id="target", status="processing")
    payload.pop("request_id", None)
    payload.pop("request_fingerprint", None)
    payload.pop("request_fingerprint_schema", None)
    payload.pop("route", None)
    write_json_file_atomic(path, payload)
    terminalize_gateway_request_file(paths, path, paths.failed if status != "done" else paths.done, "target",
        terminal_response={"id": "target", "status": status, "ok": status == "done", "response": ""})


def _count0_steer(request_id="ordinary-id"):
    return {"id": request_id, "kind": "ask", "goal": "supplement", "user_id": "tester",
            "host_request_owner.v1": {"provider": "local", "owner_kind": "main", "owner_id": "main"},
            "owner_id": "main", "conversation": {"enabled": True, "channel": "gateway",
                "channel_conversation_id": "count0-thread", "channel_user_id": "tester"},
            "metadata": {"expected_turn_id": "target"}}


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "stopped", "interrupted"])
def test_expired_steer_never_acquires_slot_or_submits(tmp_path, monkeypatch, status):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths, status)
    write_json_file_atomic(paths.inbox / "ordinary-id.json", _count0_steer())
    local = worker.GatewayAdmission()
    monkeypatch.setattr(worker, "admission", local)
    assert local.try_acquire("other", 1, 1)
    calls = []
    assert worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), lambda *_: calls.append(True)) == 0
    assert not calls
    assert local.snapshot()["total"] == 1
    terminal = read_json_file(paths.terminal / "ordinary-id.json")
    assert terminal.get("status") == "cancelled"
    assert terminal["terminal_response"]["error_code"] == "EXPIRED_TARGET_TURN"
    assert not (paths.inbox / "ordinary-id.json").exists()
    assert not (paths.processing / "ordinary-id.json").exists()
    assert "execution_attempt_id" not in terminal


def _queued_prepared_steer(context):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.tests.test_gateway_prepared_route_immutability import (
        _fingerprint,
        _prepare,
        _request,
    )

    payload = _request()
    payload["conversation"].update(channel="feishu", channel_conversation_id="fixture-thread", channel_user_id="fixture-alice")
    payload["metadata"]["expected_turn_id"] = "target"
    _fingerprint(payload)
    receipt, _ = _prepare(context.paths, payload)
    with delivery.gateway_input_transition(context.paths, receipt.request_id):
        receipt, _ = delivery.queue_gateway_input_locked(context.paths, receipt)
    return receipt


@pytest.mark.parametrize("reserved", [False, True])
def test_multiprocess_expiry_does_not_create_or_leak_reservation(context, monkeypatch, reserved):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        reservation_file,
        session_scope_for_request,
    )
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _agent = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    if reserved:
        current = read_json_file(path)
        owner = worker._durable_request_owner(gate, current)
        assert gate.admit(path, (owner, session_scope_for_request(current)), worker.AdmissionLimits(1, 1))["status"] == "admitted"
    _terminal_target(context.paths, source=receipt.prepared_request)
    calls = []
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(1, 1), lambda *_: calls.append(True), admission_gate=gate) == 0
    assert not calls
    terminal = read_json_file(context.paths.terminal / path.name)
    assert terminal.get("error_code") == "EXPIRED_TARGET_TURN"
    assert not path.exists()
    assert gate.occupied_snapshot()["total"] == 0
    reservation = reservation_file(context.paths, receipt.request_id)
    if reserved:
        assert read_json_file(reservation)["state"] == "released"
    else:
        assert not reservation.exists()


@pytest.mark.parametrize("reserved", [False, True])
def test_mp_expiry_projection_crash_retains_inbox_and_retries_canonical(context, monkeypatch, reserved):
    from agent_py_agent.agent.gateway_parts import recovery
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _ = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    if reserved:
        current = read_json_file(path)
        assert gate.admit(path, (worker._durable_request_owner(gate, current), session_scope_for_request(current)), worker.AdmissionLimits(1, 1))["status"] == "admitted"
    _terminal_target(context.paths, source=receipt.prepared_request)
    project = recovery._project_recovered_terminal_response

    def fail(*_args, **_kwargs):
        raise OSError("projection crash")

    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", fail)
    calls = []
    try:
        worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(1, 1), lambda *_: calls.append(True), admission_gate=gate)
    except OSError as exc:
        assert str(exc) == "projection crash"
    assert path.exists(), "canonical后不能在投影成功前丢掉重试责任"
    canonical = context.paths.terminal / path.name
    before = canonical.read_bytes()
    gate.release_settled()
    # 已提交结算补交不能再依赖旧目标仍然可读。
    (context.paths.terminal / "target.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", project)
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(1, 1), lambda *_: calls.append(True), admission_gate=gate) == 0
    assert not calls and not path.exists()
    assert canonical.read_bytes() == before
    assert read_json_file(context.paths.responses / path.name)["error_code"] == "EXPIRED_TARGET_TURN"
    assert read_gateway_input_receipt(context.paths, receipt.request_id).state == "terminal_unknown"
    assert gate.occupied_snapshot()["total"] == 0


def test_mp_expiry_rechecks_lease_before_projection(context, monkeypatch):
    from agent_py_agent.agent.gateway_parts import queued_steer_expiry as expiry
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _ = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    _terminal_target(context.paths, source=receipt.prepared_request)
    commit = expiry.expire_queued_steer_locked

    def revoked(*args, **kwargs):
        response = commit(*args, **kwargs)
        lease = context._coordinator_role_lease
        with lease._lifecycle_gate:
            lease._closing = True
        return response

    monkeypatch.setattr(expiry, "expire_queued_steer_locked", revoked)
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(1, 1), lambda *_: pytest.fail("expired must not execute"), admission_gate=gate) == 0
    assert (context.paths.terminal / path.name).exists()
    assert path.exists()
    assert not (context.paths.responses / path.name).exists(), "失权后不能继续写客户端投影"


@pytest.mark.parametrize("field", ["host_request_owner.v1", "owner_id", "route"])
def test_canonical_resume_rejects_transport_identity_drift(context, monkeypatch, field):
    from agent_py_agent.agent.gateway_parts import queued_steer_expiry as expiry
    from agent_py_agent.agent.gateway_parts import recovery
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _ = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    _terminal_target(context.paths, source=receipt.prepared_request)
    project = recovery._project_recovered_terminal_response
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", lambda *_: (_ for _ in ()).throw(OSError("crash")))
    assert worker.dispatch_pending_requests(context.paths, worker.AdmissionLimits(1, 1), lambda *_: pytest.fail("must not execute"), admission_gate=gate) == 0
    assert path.exists()
    canonical_path = context.paths.terminal / path.name
    canonical = read_json_file(canonical_path)
    canonical[field] = ({"provider": "local", "owner_kind": "main", "owner_id": "main"} if field == "host_request_owner.v1"
                        else "other-owner" if field == "owner_id" else {"c_run_id": "other-generation"})
    write_json_file_atomic(canonical_path, canonical)
    before = canonical_path.read_bytes()
    monkeypatch.setattr(recovery, "_project_recovered_terminal_response", project)
    assert expiry.resume_expired_steer(context.paths, path, admission_gate=gate) == "blocked"
    assert path.exists() and canonical_path.read_bytes() == before
    assert not (context.paths.responses / path.name).exists()


@pytest.mark.parametrize("field", ["host_request_owner.v1", "owner_id", "conversation", "missing_owner"])
def test_target_terminal_requires_same_owner_and_session(context, field):
    import copy

    from agent_py_agent.agent.gateway_parts.queued_steer_expiry import queued_steer_expiry_response
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    receipt = _queued_prepared_steer(context)
    target = copy.deepcopy(receipt.prepared_request)
    if field == "missing_owner":
        target.pop("host_request_owner.v1")
    else:
        target[field] = ({"provider": "local", "owner_kind": "main", "owner_id": "main"} if field == "host_request_owner.v1"
                         else "other-owner" if field == "owner_id" else {"enabled": True, "id": "other-thread"})
    _terminal_target(context.paths, source=target)
    with pytest.raises(DataCorruptionError, match="scope"):
        queued_steer_expiry_response(context.paths, receipt.prepared_request)
    assert not (context.paths.terminal / (receipt.request_id + ".json")).exists()


def test_unsupported_legacy_gate_does_not_commit_expired_canonical(context, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
    from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _ = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    _terminal_target(context.paths, source=receipt.prepared_request)
    gate._runtime_takeover = None
    gate._runtime_takeover_required = False
    current = read_json_file(path)
    result = gate.admit(path, (frozen_request_owner(current), session_scope_for_request(current)), AdmissionLimits(1, 1))
    assert result["status"] == "blocked"
    assert path.exists() and not (context.paths.terminal / path.name).exists()


def test_recovery_requeue_is_not_classified_as_never_started_expiry(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queued_steer_expiry import queued_steer_pre_acquire

    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths)
    request = _count0_steer()
    request["active_turn_recovery"] = {"source": "original attempt"}
    assert queued_steer_pre_acquire(paths, request) == ""


def test_one_count0_projection_failure_does_not_abort_remaining_dispatch(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import queued_steer_expiry as expiry
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths)
    first = _count0_steer()
    first["created_at"] = 1.0
    second = _count0_steer("next-request")
    second["metadata"] = {}
    second["created_at"] = 2.0
    write_json_file_atomic(paths.inbox / "ordinary-id.json", first)
    write_json_file_atomic(paths.inbox / "next-request.json", second)
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    monkeypatch.setattr(expiry, "project_expired_steer", lambda *_: (_ for _ in ()).throw(OSError("crash")))
    calls = []

    def submit(claim, user_key, conversation_key):
        calls.append(claim.request_id)
        worker.admission.release(user_key, conversation_key=conversation_key)

    assert worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), submit) == 1
    assert calls == ["next-request"]
    assert (paths.inbox / "ordinary-id.json").exists()


def test_legacy_recovery_requeue_is_not_expired(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queued_steer_expiry import queued_steer_pre_acquire
    from agent_py_agent.agent.gateway_parts.request_binding import (
        gateway_request_is_active_turn_recovery,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths)
    request = _count0_steer()
    request.update(priority="recovery", requeued_at=1.0)
    assert gateway_request_is_active_turn_recovery(request, request["id"])
    assert queued_steer_pre_acquire(paths, request) == ""


@pytest.mark.parametrize("projection_folder", ["failed", "done"])
def test_count0_terminal_projection_conflict_keeps_scanning_and_retries(tmp_path, monkeypatch, projection_folder):
    from agent_py_agent.agent.gateway_parts import request_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_gateway_request_payload

    paths = gateway_paths_from_root(tmp_path / "gateway")
    _terminal_target(paths)
    first = _count0_steer()
    first["created_at"] = 1.0
    second = _count0_steer("next-request")
    second.update(created_at=2.0, metadata={})
    path = paths.inbox / "ordinary-id.json"
    write_json_file_atomic(path, first)
    write_json_file_atomic(paths.inbox / "next-request.json", second)
    conflict = getattr(paths, projection_folder) / path.name
    write_json_file_atomic(conflict, _terminal_gateway_request_payload(first["id"], first, {
        "id": first["id"], "ok": False, "status": "failed", "response": "", "error_code": "PREVIOUS_FAILURE",
    }))
    before = conflict.read_bytes()
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    calls, errors, counts = [], [], []

    def submit(claim, user_key, conversation_key):
        calls.append(claim.request_id)
        worker.admission.release(user_key, conversation_key=conversation_key)

    for _ in range(2):
        try:
            counts.append(worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), submit))
        except RuntimeError as exc:
            errors.append(str(exc))
    assert not errors, errors
    assert counts == [1, 0] and calls == ["next-request"]
    assert path.exists() and conflict.read_bytes() == before
    assert read_json_file(paths.terminal / path.name)["error_code"] == "EXPIRED_TARGET_TURN"
    assert not (paths.responses / path.name).exists()
    assert worker.admission.snapshot()["total"] == 0


def test_mp_terminal_projection_conflict_is_blocked_before_and_after_canonical(context, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
    from agent_py_agent.agent.gateway_parts.queued_steer_expiry import resume_expired_steer
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_gateway_request_payload
    from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
    from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate

    gate, _ = _gate(context, monkeypatch)
    receipt = _queued_prepared_steer(context)
    path = context.paths.inbox / (receipt.request_id + ".json")
    _terminal_target(context.paths, source=receipt.prepared_request)
    current = read_json_file(path)
    conflict = context.paths.failed / path.name
    write_json_file_atomic(conflict, _terminal_gateway_request_payload(path.stem, current, {
        "id": path.stem, "ok": False, "status": "failed", "response": "", "error_code": "PREVIOUS_FAILURE",
    }))
    before = conflict.read_bytes()
    results, errors = [], []
    try:
        results.append(gate.admit(path, (frozen_request_owner(current), session_scope_for_request(current)), AdmissionLimits(1, 1))["status"])
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        results.append(resume_expired_steer(context.paths, path, admission_gate=gate))
    except RuntimeError as exc:
        errors.append(str(exc))
    assert not errors, errors
    assert results == ["blocked", "blocked"]
    assert path.exists() and conflict.read_bytes() == before
    assert gate.occupied_snapshot()["total"] == 0
    assert not (context.paths.responses / path.name).exists()
