"""原真实claim与P3/P5短门：只使用临时文件、规范HomePaths和可控Python线程。"""
import json
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.core import configure_canonical_background_contracts
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.support.multiprocess_agent_cases import execution_case
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


@pytest.fixture
def actual(tmp_path, monkeypatch):
    target = (tmp_path / "isolated-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    configure_canonical_background_contracts()
    binding, request = execution_case(tmp_path, monkeypatch)
    root = binding.snapshot.bootstrap.launch.owner_root
    owner = OwnerIdentity.provider_user("synthetic", "alice")
    # 只有业务对象为替身；scope消费的是原claim、P3字节、P5快照及真实typed HomePaths。
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner)))
    return binding, request, agent


def _transition(binding):
    return GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)


def test_real_worker_claim_preserves_original_fence_and_executes_only_in_bound_scope(actual):
    binding, request, agent = actual
    assert request["status"] == "processing"
    assert request["execution_attempt_id"] == binding.claim.execution_attempt_id
    assert request["lease_epoch"] == binding.claim.lease_epoch == 1
    assert not binding.snapshot.request_path.exists()
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        assert transition("submit", lambda: "same-original-worker") == "same-original-worker"
    with pytest.raises(WorkerDispatchError):
        transition("submit", lambda: pytest.fail("closed scope executed"))


def test_worker_without_original_claim_snapshot_cannot_move_pending(actual):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request

    binding, _request, _agent = actual
    paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
    path = paths.inbox / "different-pending.json"
    path.write_bytes(binding.snapshot.request_bytes)
    before = path.read_bytes()
    with pytest.raises(WorkerDispatchError, match="claim_snapshot_required"):
        claim_request(paths, path)
    assert path.read_bytes() == before and not (paths.processing / path.name).exists()


@pytest.mark.parametrize("field", ["execution_attempt_id", "lease_epoch", "route"])
def test_callback_rechecks_actual_claim_before_original_side_effect(actual, field):
    binding, request, agent = actual
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        if field == "route":
            request["route"]["c_run_id"] = "another-c"
        elif field == "lease_epoch":
            request[field] += 1
        else:
            request[field] = "another-attempt"
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises((WorkerDispatchError, InterruptedError)):
            transition("submit", lambda: pytest.fail("changed source executed"))


def test_cancel_keeps_original_ack_but_refuses_new_submit(actual):
    binding, request, agent = actual
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        request.update(cancel_requested=True, turn_phase="closing")
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises(InterruptedError):
            transition("submit", lambda: pytest.fail("cancelled request submitted"))
        assert transition("acknowledge", lambda: "same-attempt-cleanup") == "same-attempt-cleanup"


def test_copied_context_does_not_extend_original_execution_lifetime(actual):
    binding, _request, agent = actual
    with worker_execution_scope(binding, agent):
        captured = copy_context()
        transition = _transition(binding)
        assert captured.run(transition, "submit", lambda: "live") == "live"
    with pytest.raises(WorkerDispatchError, match="execution_scope_unavailable"):
        captured.run(transition, "submit", lambda: pytest.fail("copied expired source executed"))


def test_default_count_zero_retains_original_transition_without_worker_scope(actual, monkeypatch):
    binding, _request, _agent = actual
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    assert _transition(binding)("submit", lambda: "original-count-zero") == "original-count-zero"


# LLM: 原control唯一Root先登记实际gate；原P3/C claim/执行scope仍真实，ack/terminal不得因Root closing被新准入门拦。
# 函数用途: 为同一临时C准备原合法claim并保留其唯一Root来源，供业务短事务回归消费。
@pytest.fixture
def c_actual(context):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        coordinator_admission_gate,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits

    configure_canonical_background_contracts()
    ensure_gateway_folders(context.paths)
    payload = {"id": "actual-c-input", "kind": "ask", "goal": "合成C请求",
               "user_id": "local-agent", "metadata": {"channel": "chat", "user_id": "local-agent",
               "client_input_digest": "actual-c-digest"}}
    with delivery.gateway_input_transition(context.paths, payload["id"]):
        receipt, _ = delivery.load_or_prepare_gateway_input_locked(context.paths,
            request_id=payload["id"], client_input_digest="actual-c-digest", client_message_id="",
            guidance_dedupe_key="", prepared_request=payload)
        delivery.queue_gateway_input_locked(context.paths, receipt)
    # 业务对象仅提供原类型HomePaths；claim、P3、lease/controller和原快照全部走真实产品写者。
    agent = SimpleNamespace(config=context.config, home_paths=home_paths(context.home_root))
    gate = coordinator_admission_gate(agent, context.paths, SimpleNamespace(coordinator_worker_count=2))
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    # 正式gate须沿该原control登记唯一Root；原claim/业务对象及所有断言保持。
    runtime = getattr(context, "drain_runtime", None) or GatewayDrainRuntime(context)
    runtime.retain_admission(gate)
    decision = gate.admit(context.paths.inbox / (payload["id"] + ".json"),
        (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)), AdmissionLimits(8, 8))
    assert decision["status"] == "admitted"
    snapshot = freeze_coordinator_claim(gate, decision)
    claim = claim_request(context.paths, context.paths.inbox / (payload["id"] + ".json"),
                          coordinator_snapshot=snapshot)
    assert claim is not None and claim.coordinator_snapshot is snapshot
    return gate, claim, agent


def test_actual_c_claim_and_scope_use_same_original_lease_and_snapshot(c_actual):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )

    gate, claim, agent = c_actual
    with coordinator_execution_scope(gate, claim, agent):
        transition = _transition(SimpleNamespace(claim=claim))
        assert transition("submit", lambda: "same-C-claim") == "same-C-claim"
    with pytest.raises(WorkerDispatchError, match="execution_scope_unavailable"):
        transition("submit", lambda: pytest.fail("C scope exited"))


def test_actual_worker_background_gate_consumes_real_claim_scope(actual):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    binding, request, agent = actual
    context = {"agent": agent, "request": request, "request_path": binding.claim.path,
               "request_id": binding.claim.request_id}
    with worker_execution_scope(binding, agent), bridge.background_request_scope(context):
        assert bridge.require_background_model_admission() is None
        request.update(cancel_requested=True, turn_phase="closing")
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            bridge.require_background_model_admission()


def test_actual_c_background_gate_consumes_real_claim_scope(c_actual):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )

    gate, claim, agent = c_actual
    request = json.loads(claim.path.read_bytes())
    context = {"agent": agent, "request": request, "request_path": claim.path, "request_id": claim.request_id}
    with coordinator_execution_scope(gate, claim, agent), bridge.background_request_scope(context):
        assert bridge.require_background_model_admission() is None


def test_unrelated_owner_claim_does_not_wait_on_blocked_request_transition(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.gateway_parts import queue_service
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition, write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    queue_service.ensure_gateway_folders(paths)
    req_a = write_gateway_request(paths, {"id": "request-a", "user_id": "owner-a", "prompt": "a", "status": "pending", "created_at": 1.0})
    req_b = write_gateway_request(paths, {"id": "request-b", "user_id": "owner-b", "prompt": "b", "status": "pending", "created_at": 1.0})

    a_transition_held = threading.Event()
    a_claimer_waiting = threading.Event()
    release_a_transition = threading.Event()
    done_a = threading.Event()
    done_b = threading.Event()
    claims = {}
    errors = []
    original_transition = queue_service.gateway_turn_transition

    def observed_transition(gw_paths, request_id, *, blocking=True):
        if request_id == "request-a":
            a_claimer_waiting.set()
        return original_transition(gw_paths, request_id, blocking=blocking)

    monkeypatch.setattr(queue_service, "gateway_turn_transition", observed_transition)

    def hold_a():
        try:
            with gateway_turn_transition(paths, "request-a"):
                a_transition_held.set()
                assert release_a_transition.wait(5)
        except BaseException as exc:  # pragma: no cover - surfaced by errors assertion
            errors.append(("holder_a", repr(exc)))

    def claim_a():
        try:
            claims["a"] = queue_service.claim_request(paths, req_a)
        except BaseException as exc:  # pragma: no cover - surfaced by errors assertion
            errors.append(("claim_a", repr(exc)))
        finally:
            done_a.set()

    def claim_b():
        try:
            claims["b"] = queue_service.claim_request(paths, req_b)
        except BaseException as exc:  # pragma: no cover - surfaced by errors assertion
            errors.append(("claim_b", repr(exc)))
        finally:
            done_b.set()

    t_hold = threading.Thread(target=hold_a)
    t_a = threading.Thread(target=claim_a)
    t_b = threading.Thread(target=claim_b)
    t_hold.start()
    try:
        assert a_transition_held.wait(5)
        t_a.start()
        assert a_claimer_waiting.wait(5)
        assert not done_a.is_set()

        t_b.start()
        assert done_b.wait(5), "unrelated owner B claim waited on owner A transition"
        assert not done_a.is_set()
        assert claims["b"] is not None
        assert claims["b"].request_id == "request-b"
        assert claims["b"].lease_epoch == 1
        assert claims["b"].execution_attempt_id
        assert (paths.processing / "request-b.json").is_file()
        assert not req_b.exists()
    finally:
        release_a_transition.set()
        for worker in (t_hold, t_a, t_b):
            if worker.is_alive() or worker.ident is not None:
                worker.join(timeout=5)
                assert not worker.is_alive()

    assert errors == []
    assert done_a.is_set()
    assert claims["a"] is not None
    assert claims["a"].request_id == "request-a"
    assert claims["a"].lease_epoch == 1
    assert (paths.processing / "request-a.json").is_file()
    assert not req_a.exists()


def test_concurrent_claims_for_same_request_yield_at_most_one_winner_and_exact_epoch(tmp_path):
    import threading

    from agent_py_agent.agent.gateway_parts import queue_service
    from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    queue_service.ensure_gateway_folders(paths)
    req_path = write_gateway_request(
        paths,
        {
            "id": "shared-req",
            "user_id": "owner-a",
            "prompt": "race",
            "status": "pending",
            "lease_epoch": 2,
            "created_at": 1.0,
            "route": {"worker": "C", "c_run_id": "run-1"},
        },
    )

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results = [None] * worker_count
    errors = []

    def racer(idx):
        try:
            barrier.wait(timeout=5)
            results[idx] = queue_service.claim_request(paths, req_path)
        except BaseException as exc:  # pragma: no cover - surfaced by errors assertion
            errors.append((idx, repr(exc)))

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert errors == []
    winners = [claim for claim in results if claim is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert winner.request_id == "shared-req"
    assert winner.lease_epoch == 3
    assert winner.execution_attempt_id.startswith("gateway-attempt-")
    assert winner.lease_owner == winner.execution_attempt_id
    assert not req_path.exists()

    persisted = read_json_file_report(paths.processing / "shared-req.json").payload
    assert persisted["status"] == "processing"
    assert persisted["lease_epoch"] == 3
    assert persisted["execution_attempt_id"] == winner.execution_attempt_id
    assert persisted["attempts"] == 1
    assert "queue_transport_source" not in persisted

    # Terminal existence check still blocks claiming even if a duplicate inbox file exists.
    term_req = write_gateway_request(paths, {"id": "term-req", "user_id": "owner-a", "prompt": "done", "status": "pending", "created_at": 1.0})
    (paths.terminal / "term-req.json").write_text('{"id": "term-req", "status": "done"}', encoding="utf-8")
    assert queue_service.claim_request(paths, term_req) is None
    assert term_req.exists()


def test_claim_request_preserves_transition_before_json_body_lock_order(tmp_path, monkeypatch):
    from agent_py_agent.agent.concurrency import lock_order
    from agent_py_agent.agent.concurrency.lock_order import LockLevel
    from agent_py_agent.agent.gateway_parts import queue_service
    from agent_py_agent.agent.gateway_parts.io import write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    queue_service.ensure_gateway_folders(paths)
    req_path = write_gateway_request(
        paths,
        {
            "id": "ordered-req",
            "user_id": "owner-a",
            "prompt": "order",
            "status": "pending",
            "created_at": 1.0,
            "route": {"worker": "C", "c_run_id": "run-1"},
        },
    )

    observed_locks = []
    original_read = queue_service.read_json_file_report
    original_write = queue_service.write_json_file_atomic

    def traced_read(path, *args, **kwargs):
        observed_locks.append(("read", path.name, [entry.level for entry in lock_order.held_locks()]))
        return original_read(path, *args, **kwargs)

    def traced_write(path, payload, *args, **kwargs):
        observed_locks.append(("write", path.name, [entry.level for entry in lock_order.held_locks()]))
        return original_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(queue_service, "read_json_file_report", traced_read)
    monkeypatch.setattr(queue_service, "write_json_file_atomic", traced_write)

    before_violations = lock_order.lockdep_snapshot()["violations"]
    claim = queue_service.claim_request(paths, req_path)
    assert claim is not None
    assert lock_order.lockdep_snapshot()["violations"] == before_violations
    assert [(op, name) for op, name, _ in observed_locks] == [
        ("read", "ordered-req.json"),
        ("write", "ordered-req.json"),
        ("write", "ordered-req.json"),
    ]
    for _op, _name, held_levels in observed_locks:
        assert LockLevel.T in held_levels


def test_claim_request_rollback_on_atomic_write_oserror_allows_subsequent_clean_claim(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import queue_service
    from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    queue_service.ensure_gateway_folders(paths)
    req_path = write_gateway_request(
        paths,
        {
            "id": "rollback-req",
            "user_id": "owner-a",
            "prompt": "rollback",
            "status": "pending",
            "created_at": 1.0,
            "route": {"worker": "C", "c_run_id": "run-1"},
        },
    )
    processing_path = paths.processing / "rollback-req.json"
    original_write = queue_service.write_json_file_atomic

    def failing_processing_write(path, payload, *args, **kwargs):
        if path == processing_path:
            raise OSError("simulated I/O error")
        return original_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(queue_service, "write_json_file_atomic", failing_processing_write)
    assert queue_service.claim_request(paths, req_path) is None
    assert req_path.exists()
    assert not processing_path.exists()
    rolled_back = read_json_file_report(req_path).payload
    assert "queue_transport_source" not in rolled_back
    assert rolled_back["status"] == "pending"

    monkeypatch.undo()
    claim = queue_service.claim_request(paths, req_path)
    assert claim is not None
    assert claim.lease_epoch == 1
    assert processing_path.is_file()
    assert not req_path.exists()


def test_claim_transition_weak_locks_do_not_leak_after_distinct_requests(tmp_path):
    import gc
    import hashlib

    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.gateway_parts import queue_service
    from agent_py_agent.agent.gateway_parts.io import write_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path / "gateway")
    queue_service.ensure_gateway_folders(paths)
    transition_keys = []
    for idx in range(20):
        req_id = f"weak-req-{idx}"
        req_path = write_gateway_request(paths, {"id": req_id, "user_id": f"owner-{idx}", "prompt": "hi", "status": "pending", "created_at": 1.0})
        claim = queue_service.claim_request(paths, req_path)
        assert claim is not None and claim.request_id == req_id
        digest = hashlib.sha256(req_id.encode("utf-8")).hexdigest()
        transition_keys.append(str((paths.root / "turn_transitions" / f"{digest}.transition").resolve()))

    gc.collect()
    assert all(key not in json_io._JSON_FILE_LOCKS for key in transition_keys)
