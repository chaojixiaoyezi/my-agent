# LLM: 原C/P3/claim/短操作/terminal writer真实，只有kernel peer/P4观察边界为受控fake；无服务、Agent、网络或模型。同步首拒分责及wire回归。
# 模块用途: 验可证首次拒绝的终态/P3/原Future核收与故障窗口，输入pending不冒充P6。
import copy
import json
import os
import threading
from types import SimpleNamespace
from weakref import WeakValueDictionary

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as ma
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import multiprocess_worker_commit as producer
from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import bind_claimed_request
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
    ClaimCandidate,
    freeze_claim_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_native_commit_session import (
    NativeCommitSession,
)
from agent_py_agent.agent.gateway_parts.multiprocess_native_materialize import (
    register_owner_materialization,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_native_commit_rejection_receipt import endpoint as endpoint


# LLM: 原cold C组合根/Root/gate/P3与原worker claim均为真实临时账；fake只替代无服务的OS peer/P4调用，原begin/所有末写不mock。
# 函数用途: 建立一个新owner请求及其首次materialize原对象，准备原隔离拒绝分支。
@pytest.fixture
def case(context, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery

    ensure_gateway_folders(context.paths)
    payload = {
        "id": "native-first",
        "kind": "background",
        "goal": "合成请求",
        "user_id": "alice",
        "metadata": {
            "channel": "synthetic",
            "user_id": "alice",
            "client_input_digest": "first-digest",
        },
    }
    with delivery.gateway_input_transition(context.paths, payload["id"]):
        receipt, _ = delivery.load_or_prepare_gateway_input_locked(
            context.paths,
            request_id=payload["id"],
            client_input_digest="first-digest",
            client_message_id="",
            guidance_dedupe_key="",
            prepared_request=payload,
        )
        delivery.queue_gateway_input_locked(context.paths, receipt)
    owner = OwnerIdentity.provider_user("synthetic", "alice")
    agent = SimpleNamespace(config=context.config, home_paths=home_paths(context.home_root))
    gate = ma.coordinator_admission_gate(
        agent, context.paths, SimpleNamespace(coordinator_worker_count=2)
    )
    runtime = GatewayDrainRuntime(context)
    runtime.retain_admission(gate)
    path = context.paths.inbox / "native-first.json"
    decision = gate.admit(
        path, (owner, ma.session_scope_for_request(receipt.prepared_request)), AdmissionLimits(8, 8)
    )
    assert decision["status"] == "admitted"
    stamped = json.loads(path.read_text())
    from agent_py_agent.agent.gateway_parts.multiprocess_mode import WorkerLaunch

    launch = WorkerLaunch(
        WorkerPartition(stamped["route"]["worker"], 2, context.c_run_id),
        context.paths.root,
        context.home_root,
        os.getpid(),
        "same-native-worker",
    )
    monkeypatch.setattr(mode, "_WORKER_PARTITION", launch.partition)
    frozen = freeze_worker_bootstrap(launch)
    reservation = ma._read_reservation(ma.reservation_file(context.paths, payload["id"]))
    snapshot = freeze_claim_snapshot(frozen, ClaimCandidate(path, stamped, reservation))
    claim = claim_request(context.paths, path, worker_snapshot=snapshot)
    current = json.loads(claim.path.read_text())
    binding = bind_claimed_request(snapshot, claim, current)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    issuer = producer.WorkerCommitIssuer.__new__(producer.WorkerCommitIssuer)
    issuer.control, issuer.launch = context, launch
    issuer._controller, issuer._lease = context.controller(), runtime._lease_origin
    issuer._issuer_origin, issuer._creator_pid = issuer, os.getpid()
    issuer._source_origin = (launch, object())
    issuer._identity = {
        "schema_version": "multiprocess_process_identity.v1",
        **build_process_identity(),
    }
    issuer._pins, issuer._executor_records = set(), {}
    issuer._require_current = lambda: runtime.require_original()
    issuer._require_worker = lambda peer: None if peer == os.getpid() else pytest.fail("wrong peer")
    session = NativeCommitSession.__new__(NativeCommitSession)
    session._sources, session._lock = (issuer, object(), object()), threading.RLock()
    session._closed, session._handles, session._operations = False, {}, WeakValueDictionary()
    session._require_original = lambda: runtime.require_original()
    identifier = register_owner_materialization(
        session,
        {"request_id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id},
    )
    operation = session._operations[identifier]
    issuer._controller._unproven_owners.add(stamped["route"]["owner_sha"])
    return SimpleNamespace(
        control=context,
        gate=gate,
        runtime=runtime,
        issuer=issuer,
        session=session,
        operation=operation,
        binding=binding,
        original=current,
        owner=owner,
    )


# LLM: 原apply/原begin决定及本次token均真实；不直接构造typed producer事件或跳过原T。
# 函数用途: 执行一次确定的隔离首拒，返回原异常以观察真实失败及释放责任。
def reject(c):
    with pytest.raises(producer.WorkerCommitNotStarted) as caught:
        c.operation.apply(
            os.getpid(), lambda pin: pytest.fail("refusal pinned"), rejection_origin=object()
        )
    return caught.value


def test_first_refusal_commits_original_failed_release_and_response_but_keeps_inputs_pending(case):
    c = case
    error = reject(c)
    paths, selected = c.control.paths, c.binding.claim.request_id
    assert (paths.terminal / (selected + ".json")).exists()
    from agent_py_agent.agent.gateway_parts import multiprocess_native_refusal_source as source

    canonical = source.read_claim_payload(paths, paths.terminal / (selected + ".json"))
    assert canonical["status"] == "failed" and canonical["ok"] is False
    assert canonical["terminal_response"]["error_code"] == "WORKER_COMMIT_REJECTED"
    assert canonical["lease_process_identity"] == c.original["lease_process_identity"]
    assert not c.binding.claim.path.exists()
    assert ma._read_reservation(ma.reservation_file(paths, selected))["state"] == "released"
    assert selected not in c.issuer._controller._accepted
    from agent_py_agent.agent.gateway_parts.io import gateway_response_path
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_projection_marker_path

    assert (
        json.loads(gateway_response_path(paths, selected).read_text())
        == canonical["terminal_response"]
    )
    assert (
        json.loads(_terminal_projection_marker_path(paths, selected).read_text())[
            "settlement_pending"
        ]
        is True
    )
    assert error._terminal_release_observation == {"released": True}
    assert c.issuer._pins == set()


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "epoch",
        "birth",
        "owner",
        "route",
        "fingerprint",
        "runtime_authority",
        "conversation_claim",
        "old_claim",
        "unknown_epoch",
    ],
)
def test_full_claim_conflict_never_writes_terminal_or_releases(case, change):
    c = case
    row = copy.deepcopy(c.original)
    if change == "attempt":
        row["execution_attempt_id"] = "other"
    elif change == "epoch":
        row["lease_epoch"] = 2
    elif change == "birth":
        row["lease_process_identity"]["start_time"] = "wrong-birth"
    elif change == "owner":
        row["lease_owner"] = "other"
    elif change == "route":
        row["route"]["c_run_id"] = "other-c"
    elif change == "fingerprint":
        row["request_fingerprint"] = "0" * 64
    elif change in ("runtime_authority", "conversation_claim"):
        row[change] = {}
    elif change == "old_claim":
        row["attempts"] = 2
    else:
        row["lease_epoch"] = True
    c.binding.claim.path.write_text(json.dumps(row))
    with pytest.raises((producer.WorkerCommitRejected, InterruptedError)):
        c.operation.apply(os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object())
    assert not (c.control.paths.terminal / "native-first.json").exists()
    assert (
        ma._read_reservation(ma.reservation_file(c.control.paths, "native-first"))["state"]
        == "occupied"
    )


@pytest.mark.parametrize(
    "change",
    ["registry", "pin", "executor", "accepted", "gate", "gate_copy", "root_tuple", "revoked"],
)
def test_missing_original_responsibility_or_current_source_does_not_terminalize(case, change):
    c = case
    controller = c.issuer._controller
    if change == "registry":
        key = next(iter(controller._native_short_operations))
        controller._native_short_operations[(*key[:4], "request_commit")] = object()
    elif change == "pin":
        c.issuer._pins.add(object())
    elif change == "executor":
        c.issuer._executor_records["unknown"] = "0" * 64
    elif change == "accepted":
        controller._accepted["native-first"] = "0" * 64
    elif change == "gate":
        controller._native_terminal_admission_origin = object()
    elif change == "gate_copy":
        controller._native_terminal_admission_origin = copy.copy(c.gate)
    elif change == "root_tuple":
        c.gate._ordinary_admission_origin = (
            c.runtime,
            (*c.runtime._admission_origins[0],),
        )
    else:
        controller._recheck_stop.set()
    with pytest.raises((producer.WorkerCommitRejected, ValueError)):
        c.operation.apply(os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object())
    assert not (c.control.paths.terminal / "native-first.json").exists()
    assert (
        ma._read_reservation(ma.reservation_file(c.control.paths, "native-first"))["state"]
        == "occupied"
    )


def test_release_failure_keeps_canonical_and_original_terminal_based_retry(case, monkeypatch):
    c = case
    original = ma._write_gate_record_durable

    def fail(gate, path, payload):
        if payload["state"] == "released":
            raise OSError("synthetic-release-failure")
        return original(gate, path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(ma, "_write_gate_record_durable", fail)
        error = reject(c)
    assert error._terminal_release_observation == {
        "released": False,
        "reason": "release_write_failed",
    }
    assert (
        ma._read_reservation(ma.reservation_file(c.control.paths, "native-first"))["state"]
        == "occupied"
    )
    assert (c.control.paths.terminal / "native-first.json").exists()
    assert ma.release_reservation_if_terminal(c.gate, "native-first")["released"] is True


def test_precanonical_failure_preserves_processing_p3_and_same_exception(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import recovery

    error = OSError("synthetic-canonical-failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(recovery, "_terminalize_gateway_request_locked", fail)
    with pytest.raises(OSError) as caught:
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert caught.value is error
    assert case.binding.claim.path.exists()
    assert (
        ma._read_reservation(ma.reservation_file(case.control.paths, "native-first"))["state"]
        == "occupied"
    )


def test_revocation_after_canonical_preserves_terminal_and_refuses_response_projection(
    case, monkeypatch
):
    from agent_py_agent.agent.gateway_parts import multiprocess_native_refusal_terminal as terminal

    original = terminal.project_first_refusal

    def revoke(outcome):
        case.issuer._controller._recheck_stop.set()
        return original(outcome)

    monkeypatch.setattr(terminal, "project_first_refusal", revoke)
    with pytest.raises(producer.WorkerCommitRejected, match="projection_unavailable"):
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert (case.control.paths.terminal / "native-first.json").exists()
    from agent_py_agent.agent.gateway_parts.io import gateway_response_path

    assert not gateway_response_path(case.control.paths, "native-first").exists()


def test_first_gate_locator_is_never_replaced_by_later_legitimate_gate(case):
    second = ma.coordinator_admission_gate(
        case.gate.agent, case.control.paths, SimpleNamespace(coordinator_worker_count=2)
    )
    case.runtime.retain_admission(second)
    from agent_py_agent.agent.gateway_parts import multiprocess_native_refusal_source as source

    assert source.original_terminal_gate(case.issuer) is case.gate
    assert len(case.runtime._admission_origins) == 2


def test_nack_same_call_consumes_only_matching_original_canonical_without_agent(
    case, endpoint, monkeypatch
):
    from agent_py_agent.agent.gateway_parts.multiprocess_native_commit_client import (
        NativeCommitNotStarted,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_native_refusal_consume import (
        consume_first_refusal,
    )

    reject(case)
    token = object()
    endpoint.frames.append(
        (
            {"op": "rejected", "seq": 1, "error_code": "WORKER_COMMIT_REJECTED"},
            endpoint.value._parent[0],
        )
    )
    with pytest.raises(NativeCommitNotStarted) as caught:
        endpoint.value.materialize_owner(
            "native-first", case.binding.claim.execution_attempt_id, call_origin=token
        )
    monkeypatch.setattr(mode, "_WORKER_PARTITION", case.binding.snapshot.bootstrap.launch.partition)
    assert (
        consume_first_refusal(case.binding, case.original, endpoint.value, caught.value, token)
        is True
    )
    assert (
        consume_first_refusal(case.binding, case.original, endpoint.value, caught.value, object())
        is False
    )
    assert (
        consume_first_refusal(
            case.binding, case.original, copy.copy(endpoint.value), caught.value, token
        )
        is False
    )


def test_original_prepared_receipt_is_not_input_settlement_or_an_exit_blocker(case):
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.agent.gateway_parts.recovery import _terminal_projection_marker_path

    before = read_gateway_input_receipt(case.control.paths, "native-first")
    assert before.state == "queued" and before.target_turn_id == ""
    reject(case)
    assert read_gateway_input_receipt(case.control.paths, "native-first") == before
    assert (
        json.loads(
            _terminal_projection_marker_path(case.control.paths, "native-first").read_text()
        )["settlement_pending"]
        is True
    )


@pytest.mark.parametrize("history", ["same_attempt", "older_attempt", "invalid"])
def test_any_original_p1_for_request_or_unknown_shape_keeps_claim_debt(case, history):
    import hashlib

    from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record

    row = {
        "schema_version": "multiprocess_executor_reservation.v1",
        "reservation_id": "original-p1",
        "kind": "tool",
        "c_run_id": case.control.c_run_id,
        "owner_sha": case.original["route"]["owner_sha"],
        "request_id": "native-first",
        "execution_attempt_id": case.binding.claim.execution_attempt_id
        if history == "same_attempt"
        else "older-attempt",
        "lease_epoch": 1,
        "tool_call_id": "original-tool",
        "state": "reserved",
        "process_identity": None,
        "pgid": None,
        "sid": None,
    }
    directory = (
        case.control.paths.root / "workers" / "executors" / f"w{case.issuer.launch.partition.index}"
    )
    directory.mkdir(parents=True)
    (
        directory / (hashlib.sha256(row["reservation_id"].encode()).hexdigest() + ".json")
    ).write_bytes(b"{}" if history == "invalid" else serialize_record(row))
    with pytest.raises((producer.WorkerCommitRejected, ValueError)):
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert case.binding.claim.path.exists()
    assert not (case.control.paths.terminal / "native-first.json").exists()


def test_original_execute_claim_and_reap_end_only_the_matching_failed_future(
    case, endpoint, monkeypatch
):
    from concurrent.futures import Future

    from agent_py_agent.agent.gateway_parts.multiprocess_execution import execute_worker_claim
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump

    def send(sender, raw, **kwargs):
        request = json.loads(raw)
        assert request["op"] == "materialize" and request["request_id"] == "native-first"
        with monkeypatch.context() as patch:
            patch.setattr(mode, "_WORKER_PARTITION", None)
            reject(case)
        endpoint.frames.append(
            (
                {"op": "rejected", "seq": request["seq"], "error_code": "WORKER_COMMIT_REJECTED"},
                endpoint.value._parent[0],
            )
        )

    from agent_py_agent.agent.gateway_parts import multiprocess_native_commit_client as clients

    monkeypatch.setattr(clients.KernelMessageSender, "send", send)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", case.binding.snapshot.bootstrap.launch.partition)
    pool = SimpleNamespace(
        native_client=endpoint.value, get=lambda owner: pytest.fail("Agent constructed")
    )
    result = execute_worker_claim(case.binding.snapshot, case.binding.claim, pool)
    assert result is True
    done, pending = Future(), Future()
    done.set_result(result)
    pump = WorkerRequestPump.__new__(WorkerRequestPump)
    pump._active = {done: case.binding.claim, pending: object()}
    pump._reap()
    assert done not in pump._active and pending in pump._active
    assert endpoint.value._pending == endpoint.value._uncertain == set()


@pytest.mark.parametrize(
    "change",
    [
        "canonical_absent",
        "canonical_claim",
        "canonical_response",
        "canonical_birth",
        "copied_error",
        "old_seq",
        "wrong_call",
        "bad_p3",
    ],
)
def test_worker_token_and_canonical_must_both_match(case, endpoint, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts.multiprocess_native_commit_client import (
        NativeCommitNotStarted,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_native_refusal_consume import (
        consume_first_refusal,
    )

    reject(case)
    token = object()
    endpoint.frames.append(
        (
            {"op": "rejected", "seq": 1, "error_code": "WORKER_COMMIT_REJECTED"},
            endpoint.value._parent[0],
        )
    )
    with pytest.raises(NativeCommitNotStarted) as caught:
        endpoint.value.materialize_owner(
            "native-first", case.binding.claim.execution_attempt_id, call_origin=token
        )
    error = caught.value
    selected = case.control.paths.terminal / "native-first.json"
    if change == "canonical_absent":
        selected.unlink()
    elif change.startswith("canonical_"):
        row = json.loads(selected.read_text())
        if change == "canonical_claim":
            row["execution_attempt_id"] = "other"
        elif change == "canonical_birth":
            row["lease_process_identity"]["start_time"] = "wrong"
        else:
            row["terminal_response"]["ok"] = True
        selected.write_text(json.dumps(row))
    elif change == "copied_error":
        # BaseException copy()重建构造参数不同；显式复制实例字段以验证origin必须仍为原异常。
        clone = producer.WorkerCommitRejected.__new__(NativeCommitNotStarted)
        clone.__dict__.update(error.__dict__)
        error = clone
    elif change == "old_seq":
        endpoint.value._seq += 1
    elif change == "wrong_call":
        token = object()
    else:
        path = ma.reservation_file(case.control.paths, "native-first")
        row = json.loads(path.read_text())
        row["terminal_fact"]["record_sha256"] = "0" * 64
        path.write_text(json.dumps(row))
    monkeypatch.setattr(mode, "_WORKER_PARTITION", case.binding.snapshot.bootstrap.launch.partition)
    if change in {"canonical_claim", "canonical_response", "canonical_birth"}:
        with pytest.raises((producer.WorkerCommitRejected, ValueError)):
            consume_first_refusal(case.binding, case.original, endpoint.value, error, token)
    else:
        assert (
            consume_first_refusal(case.binding, case.original, endpoint.value, error, token)
            is False
        )


@pytest.mark.parametrize("kind", ["ordinary", "same_text"])
def test_ordinary_begin_failure_is_not_converted_even_with_first_source(case, monkeypatch, kind):
    error = (
        RuntimeError("owner_commit_unavailable")
        if kind == "ordinary"
        else producer.WorkerCommitRejected("owner_commit_unavailable")
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(case.issuer, "begin", fail)
    with pytest.raises(RuntimeError) as caught:
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert caught.value is error
    assert (
        case.binding.claim.path.exists()
        and not (case.control.paths.terminal / "native-first.json").exists()
    )


def test_nack_send_failure_retains_original_canonical_and_release_without_second_send(
    case, monkeypatch
):
    from agent_py_agent.agent.gateway_parts import multiprocess_native_commit_session as sessions

    c, session = case, case.session
    session._last_seq = 0
    request = {
        "op": "materialize",
        "seq": 1,
        "request_id": "native-first",
        "execution_attempt_id": c.binding.claim.execution_attempt_id,
    }
    monkeypatch.setattr(
        sessions,
        "receive_control_packet",
        lambda *args, **kwargs: (json.dumps(request).encode(), os.getpid()),
    )
    session._dispatch_request = lambda _request, peer, seq, op, *, rejection_origin=None: (
        c.operation.apply(
            peer, lambda pin: pytest.fail("pinned"), rejection_origin=rejection_origin
        )
    )
    error, sends = OSError("synthetic-send-failure"), []

    def send(sender, raw, **kwargs):
        sends.append(json.loads(raw))
        raise error

    monkeypatch.setattr(sessions.KernelMessageSender, "send", send)
    with pytest.raises(OSError) as caught:
        session.serve_once()
    assert caught.value is error and len(sends) == 1 and sends[0]["op"] == "rejected"
    assert (c.control.paths.terminal / "native-first.json").exists()
    assert (
        ma._read_reservation(ma.reservation_file(c.control.paths, "native-first"))["state"]
        == "released"
    )
    assert session._handles == {}


def test_first_gate_locator_cannot_switch_to_a_later_valid_gate(case):
    from agent_py_agent.agent.gateway_parts import multiprocess_native_refusal_source as source

    second = ma.coordinator_admission_gate(
        case.gate.agent, case.control.paths, SimpleNamespace(coordinator_worker_count=2)
    )
    case.runtime.retain_admission(second)
    case.issuer._controller._native_terminal_admission_origin = second
    with pytest.raises(producer.WorkerCommitRejected):
        source.original_terminal_gate(case.issuer)


def test_known_foreign_owner_pin_and_p1_do_not_claim_or_clear_that_responsibility(case):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_records import record_digest
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.tests.support.multiprocess_dispatch_cases import other_owner

    foreign = other_owner(case.original)
    pin = case.issuer.begin(foreign, peer_pid=os.getpid(), rejection_origin=object())
    ledger = ExecutorLedger(case.control.paths.root, case.issuer.launch.partition)
    row = ledger.reserve(
        {
            "schema_version": "multiprocess_executor_reservation.v1",
            "reservation_id": "foreign-p1",
            "kind": "tool",
            "c_run_id": case.control.c_run_id,
            "owner_sha": owner_digest(foreign),
            "request_id": "foreign-request",
            "execution_attempt_id": "foreign-attempt",
            "lease_epoch": 1,
            "tool_call_id": "foreign-tool",
            "state": "reserved",
            "process_identity": None,
            "pgid": None,
            "sid": None,
        }
    )
    case.issuer._executor_records[row["reservation_id"]] = record_digest(row)
    try:
        reject(case)
        assert ledger.read(row["reservation_id"]) == row
        assert pin in case.issuer._pins
        assert (
            ma._read_reservation(ma.reservation_file(case.control.paths, "native-first"))["state"]
            == "released"
        )
    finally:
        case.issuer.finish(pin)


@pytest.mark.parametrize("leaf", ["request", "p3"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink"])
def test_strict_original_source_rejects_unsafe_leaf_before_any_terminal_effect(
    case, tmp_path, leaf, unsafe
):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    target = (
        case.binding.claim.path
        if leaf == "request"
        else ma.reservation_file(case.control.paths, "native-first")
    )
    original = target.read_bytes()
    outside = tmp_path / (leaf + "-original-copy")
    outside.write_bytes(original)
    target.unlink()
    if unsafe == "symlink":
        target.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
    with pytest.raises(NoFollowPathError):
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("unsafe source pinned"), rejection_origin=object()
        )
    assert outside.read_bytes() == original
    assert not (case.control.paths.terminal / "native-first.json").exists()


def test_existing_canonical_never_becomes_a_first_refusal_overwrite(case):
    path = case.control.paths.terminal / "native-first.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b'{"unrelated_original_terminal":true}'
    path.write_bytes(content)
    with pytest.raises((producer.WorkerCommitRejected, InterruptedError)):
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert path.read_bytes() == content and case.binding.claim.path.exists()
    assert (
        ma._read_reservation(ma.reservation_file(case.control.paths, "native-first"))["state"]
        == "occupied"
    )


def test_full_first_refusal_rejects_fifo_p1_and_keeps_all_debt(case):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    directory = (
        case.control.paths.root / "workers" / "executors" / f"w{case.issuer.launch.partition.index}"
    )
    directory.mkdir(parents=True)
    os.mkfifo(directory / ("a" * 64 + ".json"))
    with pytest.raises(NoFollowPathError):
        case.operation.apply(
            os.getpid(), lambda pin: pytest.fail("pinned"), rejection_origin=object()
        )
    assert case.binding.claim.path.exists()
    assert not (case.control.paths.terminal / "native-first.json").exists()


def test_new_strict_reader_rejects_fifo_request_without_entering_legacy_transition(case):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.agent.gateway_parts.multiprocess_native_refusal_source import (
        read_claim_payload,
    )

    path = case.binding.claim.path
    path.unlink()
    os.mkfifo(path)
    # 这里只验证本片reader；原GatewayActiveTurnTransition的旧普通读取仍不是本片修复范围。
    with (
        gateway_turn_transition(case.control.paths, case.binding.claim.request_id),
        pytest.raises(NoFollowPathError),
    ):
        read_claim_payload(case.control.paths, path)
    assert not (case.control.paths.terminal / "native-first.json").exists()


def test_first_claim_proof_does_not_accept_a_self_consistent_historical_attempt(case):
    from agent_py_agent.agent.gateway_parts.multiprocess_native_refusal_source import (
        require_first_claim,
    )

    row = copy.deepcopy(case.original)
    row.update(lease_epoch=2, attempts=2)
    with pytest.raises(producer.WorkerCommitRejected, match="first_native_claim_required"):
        require_first_claim(case.issuer, case.operation._sources[2], row)
    assert case.binding.claim.path.exists()
    assert not (case.control.paths.terminal / "native-first.json").exists()
