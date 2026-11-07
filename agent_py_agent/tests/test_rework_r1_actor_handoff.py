"""真实队列交接只释放已完成的精确 actor 身份，不把让出执行当成终态。"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import conversation_actor as actor
from agent_py_agent.agent.gateway_parts import control_service as control
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.io import read_json_file, update_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import (
    GatewayArchiveDisposition,
    GatewayArchiveResult,
    ensure_gateway_folders,
)
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskParams, submit_gateway_ask
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.user_space import owner_epoch


@pytest.fixture
def lane(tmp_path, monkeypatch, request):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    agent = SimpleNamespace(config=SimpleNamespace(
        conversation_actor_workers=getattr(request, "param", 1), owner_epoch_fence_enabled=False,
    ), conversation_store=None)
    scheduler = worker.gateway_conversation_scheduler(paths, agent)
    monkeypatch.setattr(worker, "admission", worker.GatewayAdmission())
    monkeypatch.setattr(worker, "_CLAIM_EPOCHS", {})
    state = SimpleNamespace(paths=paths, agent=agent, scheduler=scheduler, root=tmp_path)
    try:
        yield state
    finally:
        if scheduler is not None:
            scheduler.close(wait=True)


def enqueue(lane):
    return submit_gateway_ask(lane.paths, params=GatewayAskParams("work", chat_session_id="conversation"))


def dispatch(lane):
    submitted = []
    count = worker.dispatch_pending_requests(
        lane.paths, worker.AdmissionLimits(5, 5), lambda *args: submitted.append(args),
    )
    assert count == 1
    claim, user, key = submitted[0]
    return claim, user, key


def drain(lane, key):
    lane.scheduler.post(actor.TurnFinished("owner", key, "barrier")).result(timeout=5)


def bind(lane, claim, *, epoch=7, provisional=False):
    payload = read_json_file(claim.path)
    key = worker.request_conversation_key(payload) or claim.request_id
    binding = (None, epoch, SimpleNamespace(owner_epoch_fence_enabled=False))
    exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
    other = worker._claim_epoch_key(claim.path, claim.request_id, "later-attempt")
    worker._CLAIM_EPOCHS[exact] = binding
    worker._CLAIM_EPOCHS[other] = binding
    if not provisional:
        assert lane.scheduler.sequencer.decide(actor.Admit(
            worker.request_user_key(payload), key, claim.request_id, claim.execution_attempt_id, epoch,
        )).action == "admit"
    return payload, key, exact, other


def finish(lane, claim, response):
    worker._finish_claimed_gateway_request(
        lane.paths, claim.path, claim.request_id, response,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )


def requeue(lane, claim, kind, monkeypatch):
    if kind == "owner":
        return worker._requeue_owner_capacity(lane.paths, claim.path, read_json_file(claim.path))
    response = {"id": claim.request_id, "status": "failed"}
    if kind == "provider":
        response["provider_transient_resume"] = {"resume_count": 1, "error_code": "PROVIDER_TIMEOUT"}
    else:
        from agent_py_agent.agent.gateway_parts import (
            multiprocess_background_execution as background,
        )
        from agent_py_agent.agent.gateway_parts.recovery import requeue_current_processing_request

        # 只替换后台消费者身份接缝；实际 T/lease CAS、写 pending 和移动仍用生产入口。
        def pending(*_):
            return requeue_current_processing_request(
                lane.paths, claim,
                GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id),
                not_before_at=worker.time.time() + 30,
            )

        monkeypatch.setattr(background, "requeue_pending_background_request", pending)
        response.update(kind="background", background_result={"outcome": "pending"})
    finish(lane, claim, response)
    return True


def test_dispatch_claim_early_stop_terminal_allows_next_request(lane, monkeypatch):
    rid, _, _ = enqueue(lane)
    claim, user, key = dispatch(lane)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (rid, "", 0)
    lane.scheduler.sequencer.decide(actor.Stop(user, key, rid))
    update_json_file_atomic(claim.path, lambda payload: dict(payload, cancel_requested=True))
    handled = []

    def interrupted(*_, **kwargs):
        handled.append(kwargs["refresh_lease"])
        return {"id": rid, "status": "interrupted", "ok": False}

    monkeypatch.setattr(worker, "_handle_gateway_request", interrupted)
    assert worker._process_claimed_gateway_request_path(lane.agent, lane.paths, claim.path, "worker", claim=claim)
    drain(lane, key)
    assert handled == [False]
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert rid not in lane.scheduler.sequencer._box(key).stopped
    worker.admission.release(user, conversation_key=key)
    next_id, _, _ = enqueue(lane)
    following, _, _ = dispatch(lane)
    assert following.request_id == next_id


@pytest.mark.parametrize("provisional", [False, True])
def test_terminal_cleans_only_its_claim_binding(lane, provisional):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim, provisional=provisional)
    finish(lane, claim, {"id": claim.request_id, "status": "done", "ok": True})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
@pytest.mark.parametrize("provisional", [False, True])
def test_successful_requeue_releases_actor_and_can_be_reclaimed(lane, monkeypatch, kind, provisional):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim, provisional=provisional)
    assert requeue(lane, claim, kind, monkeypatch)
    drain(lane, key)
    assert not claim.path.exists()
    pending = lane.paths.inbox / claim.name
    assert read_json_file(pending)["status"] == "pending"
    assert not (lane.paths.terminal / claim.name).exists()
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS
    worker.admission.release(user, conversation_key=key)
    update_json_file_atomic(pending, lambda payload: dict(payload, not_before_at=0))
    following, _, _ = dispatch(lane)
    assert following.request_id == claim.request_id
    assert following.execution_attempt_id != claim.execution_attempt_id


@pytest.mark.parametrize("target", ["new_attempt", "new_epoch", "other_request", "nonzero_provisional"])
def test_late_terminal_does_not_clear_a_different_active_tuple(lane, target):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim)
    previous = (claim.request_id, claim.execution_attempt_id, 7)
    targets = {
        "new_attempt": (claim.request_id, "later-attempt", 7),
        "new_epoch": (claim.request_id, claim.execution_attempt_id, 8),
        "other_request": ("other-request", "", 0),
        "nonzero_provisional": (claim.request_id, "", 8),
    }
    current = targets[target]
    assert lane.scheduler.sequencer.decide(actor.Recover(
        user, key, *current, exit_proof=True, previous_attempt=previous,
    )).action == "admit"
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == current
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("disposition", [
    GatewayArchiveDisposition.STALE_CLAIM, GatewayArchiveDisposition.CONFLICT,
    GatewayArchiveDisposition.RETRYABLE_IO,
])
def test_uncommitted_terminal_preserves_actor_binding_and_stop(lane, monkeypatch, disposition):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id))
    monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs: GatewayArchiveResult(disposition))
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
def test_failed_requeue_keeps_its_actor_and_binding(lane, monkeypatch, kind):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    original = Path.replace

    def fail_move(path, target):
        if path == claim.path:
            raise OSError("injected queue move failure")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail_move)
    monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs:
                        GatewayArchiveResult(GatewayArchiveDisposition.RETRYABLE_IO))
    if kind == "provider":
        requeue(lane, claim, kind, monkeypatch)
    else:
        with pytest.raises(OSError, match="queue move failure"):
            requeue(lane, claim, kind, monkeypatch)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.exists()
    assert not (lane.paths.terminal / claim.name).exists()


def test_restart_resume_retains_original_responsibility(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "restart_resume": {"cause": "shutdown"}})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before


def test_changed_claim_rejects_old_completion_without_clearing_active(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    update_json_file_atomic(claim.path, lambda payload: dict(
        payload, execution_attempt_id="replacement", lease_epoch=claim.lease_epoch + 1,
    ))
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_stale_owner_epoch_keeps_active_and_claim_binding(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    repo = RuntimeRepository(lane.root / "epoch.db")
    old = owner_epoch.activate_owner_epoch(repo)
    _, key, exact, _ = bind(lane, claim, epoch=old)
    worker._CLAIM_EPOCHS[exact] = (repo, old, SimpleNamespace(owner_epoch_fence_enabled=True))
    owner_epoch.activate_owner_epoch(repo)
    before = claim.path.read_bytes()
    finish(lane, claim, {"id": claim.request_id, "status": "done"})
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, old)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_recorded_unknown_never_releases_actor_or_claim(lane):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    error = BoundedCallExitUnconfirmedError("original-call", "worker_exit_unknown")
    observation = {
        "schema_version": "gateway_execution_outcome.v1", "status": "unknown",
        "request_id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
        "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner,
        "phase": "bounded_call_exit", "operation_entered": None,
        "error_code": error.error_code, "call_id": error.call_id, "reason": error.reason,
    }
    update_json_file_atomic(claim.path, lambda payload: dict(payload, execution_outcome=observation))
    before = claim.path.read_bytes()
    worker.terminalize_unhandled_claimed_gateway_request(
        lane.paths, claim.path, error,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
    )
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def test_owner_capacity_process_notifies_only_after_transition_exit(lane, monkeypatch):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim, provisional=True)
    entered = []
    notifications = []
    original_scope = GatewayActiveTurnTransition.scope
    original_post = lane.scheduler.post

    @contextmanager
    def scope(transition, *args):
        with original_scope(transition, *args):
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()

    def post(command, **kwargs):
        assert not entered, "actor notification must not run inside the original transition"
        notifications.append(command)
        return original_post(command, **kwargs)

    def full(*_):
        raise worker.PoolCapacityExceededError("capacity")

    monkeypatch.setattr(worker, "_resolve_request_agent", full)
    monkeypatch.setattr(GatewayActiveTurnTransition, "scope", scope)
    monkeypatch.setattr(lane.scheduler, "post", post)
    assert worker._process_claimed_gateway_request_path(lane.agent, lane.paths, claim.path, "worker", claim=claim)
    drain(lane, key)
    assert any(command.request_id == claim.request_id for command in notifications)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("kind", ["provider", "owner", "background"])
@pytest.mark.parametrize("stop_at", ["before_move", "after_move"])
def test_stop_interleaved_with_requeue_preserves_tombstone(lane, monkeypatch, kind, stop_at):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, _ = bind(lane, claim)
    ready, release = threading.Event(), threading.Event()
    original = Path.replace

    def move(path, target):
        if path != claim.path:
            return original(path, target)
        if stop_at == "after_move":
            result = original(path, target)
        ready.set()
        assert release.wait(5)
        return result if stop_at == "after_move" else original(path, target)

    monkeypatch.setattr(Path, "replace", move)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(requeue, lane, claim, kind, monkeypatch)
        try:
            assert ready.wait(5)
            assert lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id)).action == "stop"
        finally:
            release.set()
        assert pending.result(timeout=5)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, claim.request_id, "later", 7)).action == "rejected"
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, "next-request", "next", 7)).action == "admit"
    assert not (lane.paths.terminal / claim.name).exists()


def test_owner_capacity_changed_claim_cannot_release_reservation(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim, provisional=True)
    update_json_file_atomic(claim.path, lambda current: dict(current, lease_owner="changed"))
    before = claim.path.read_bytes()
    with pytest.raises(DataCorruptionError, match="claim changed"):
        worker._requeue_owner_capacity(lane.paths, claim.path, payload)
    drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, "", 0)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before


@pytest.mark.parametrize("lane", [0], indirect=True)
@pytest.mark.parametrize("kind", ["provider", "background"])
def test_disabled_actor_requeue_then_terminal_leaves_no_request_bindings(lane, monkeypatch, kind):
    assert lane.scheduler is None
    assert lane.agent.config.owner_epoch_fence_enabled is False
    lane.agent.subagents = SimpleNamespace(runtime_db=RuntimeRepository(lane.root / "disabled-epoch.db"))
    rid, _, _ = enqueue(lane)
    claim, user, key = dispatch(lane)
    binding = worker._stamp_claim_owner_epoch(
        lane.agent, claim.path, read_json_file(claim.path), claim.execution_attempt_id,
    )
    exact = worker._claim_epoch_key(claim.path, rid, claim.execution_attempt_id)
    unrelated = worker._claim_epoch_key(lane.paths.processing / "unrelated.json", "unrelated", "other-attempt")
    worker._CLAIM_EPOCHS[unrelated] = binding
    assert exact in worker._CLAIM_EPOCHS
    assert requeue(lane, claim, kind, monkeypatch)
    assert exact not in worker._CLAIM_EPOCHS
    pending = lane.paths.inbox / claim.name
    assert read_json_file(pending)["status"] == "pending"
    assert not (lane.paths.terminal / claim.name).exists()
    worker.admission.release(user, conversation_key=key)
    update_json_file_atomic(pending, lambda payload: dict(payload, not_before_at=0))
    following, _, _ = dispatch(lane)
    assert following.execution_attempt_id != claim.execution_attempt_id
    worker._stamp_claim_owner_epoch(
        lane.agent, following.path, read_json_file(following.path), following.execution_attempt_id,
    )
    assert worker._claim_epoch_key(following.path, rid, following.execution_attempt_id) in worker._CLAIM_EPOCHS
    finish(lane, following, {"id": rid, "status": "done", "ok": True})
    assert read_json_file(lane.paths.terminal / following.name)["terminal_response"]["status"] == "done"
    assert [entry for entry in worker._CLAIM_EPOCHS if entry[2] == rid] == []
    assert unrelated in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [0], indirect=True)
@pytest.mark.parametrize("outcome", ["unknown", "uncommitted", "restart_resume"])
def test_disabled_actor_unfinished_exits_keep_claim_binding(lane, monkeypatch, outcome):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    assert lane.scheduler is None
    assert lane.agent.config.owner_epoch_fence_enabled is False
    lane.agent.subagents = SimpleNamespace(runtime_db=RuntimeRepository(lane.root / "unfinished-epoch.db"))
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    worker._stamp_claim_owner_epoch(lane.agent, claim.path, read_json_file(claim.path), claim.execution_attempt_id)
    exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
    before = claim.path.read_bytes()
    if outcome == "unknown":
        worker.terminalize_unhandled_claimed_gateway_request(
            lane.paths, claim.path, BoundedCallExitUnconfirmedError("unconfirmed-call", "worker_exit_unknown"),
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch,
        )
    else:
        response = {"id": claim.request_id, "status": "done"}
        if outcome == "restart_resume":
            response["restart_resume"] = {"cause": "shutdown"}
        else:
            monkeypatch.setattr(worker, "archive_request", lambda *_args, **_kwargs:
                                GatewayArchiveResult(GatewayArchiveDisposition.RETRYABLE_IO))
        finish(lane, claim, response)
    assert exact in worker._CLAIM_EPOCHS
    assert claim.path.read_bytes() == before
    assert not (lane.paths.terminal / claim.name).exists()


def _resource_count():
    if os.name == "nt":
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    return len(os.listdir(root))


@contextmanager
def _no_resource_growth():
    before = _resource_count()
    try:
        yield
    finally:
        assert _resource_count() - before == 0, "request operation leaked descriptors/handles"


def _control_scope(payload):
    conversation = payload["conversation"]
    return control.GatewayControlScope(
        payload.get("user_id") or conversation["channel_user_id"],
        conversation["channel"], conversation["channel_conversation_id"],
    )


def _stop_entry(lane, record, scope, entry):
    if entry == "mark":
        return control._mark_request_stopping(record, scope)[0]
    return control._stop_live_window_request(lane.agent, lane.paths, record, scope, interrupt_only=True).ok


@pytest.mark.parametrize("lane", [4, 0], indirect=True)
@pytest.mark.parametrize("sealed", [False, True])
def test_projection_recovery_hands_off_exact_actor_after_one_io_error(lane, monkeypatch, sealed):
    enqueue(lane)
    claim, user, key = dispatch(lane)
    if lane.scheduler is not None:
        _, key, exact, other = bind(lane, claim)
    else:
        exact = worker._claim_epoch_key(claim.path, claim.request_id, claim.execution_attempt_id)
        other = worker._claim_epoch_key(claim.path, claim.request_id, "later-attempt")
        binding = (None, 7, lane.agent.config)
        worker._CLAIM_EPOCHS.update({exact: binding, other: binding})
    original_write = recovery.write_json_file_atomic
    failures = []

    def write(path, payload, **kwargs):
        if Path(path) == lane.paths.done / claim.name and not failures:
            failures.append(True)
            raise OSError("simulated done projection I/O error")
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(recovery, "write_json_file_atomic", write)
    response = {"id": claim.request_id, "status": "done", "ok": True, "output": "original answer"}
    with _no_resource_growth():
        before_failure_resources = _resource_count()
        if sealed:
            original_write(claim.path, recovery._terminal_gateway_request_payload(
                claim.request_id, read_json_file(claim.path), response,
            ))
            first = recovery.recover_gateway_processing_requests_report(lane.paths, agent=None)
            assert first.load_errors
        else:
            finish(lane, claim, response)
        assert _resource_count() == before_failure_resources
        assert failures == [True]
        assert exact in worker._CLAIM_EPOCHS
        canonical = lane.paths.terminal / claim.name
        before = canonical.read_bytes()
        repairs = [recovery.repair_gateway_terminal_projections(lane.paths, agent=None) for _ in range(2)]
        assert repairs == [(1, []), (0, [])]
        assert canonical.read_bytes() == before
        assert read_json_file(lane.paths.responses / claim.name) == response
        if lane.scheduler is not None:
            drain(lane, key)
            assert lane.scheduler.sequencer.snapshot(key)["active"] is None
        assert exact not in worker._CLAIM_EPOCHS
        assert other in worker._CLAIM_EPOCHS
        worker.admission.release(user, conversation_key=key)
        next_id, _, _ = enqueue(lane)
        following, _, _ = dispatch(lane)
        assert following.request_id == next_id


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_stale_stop_after_owner_requeue_does_not_block_readmission(lane, monkeypatch, entry):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    scope = _control_scope(payload)
    record = control._GatewayRequestRecord(claim.path, payload)
    selected, resume = threading.Event(), threading.Event()
    original_decision = control._control_command_decision

    def decide(paths, command_scope, kind, turn_id, **kwargs):
        if kind == "stop":
            selected.set()
            assert resume.wait(5)
        return original_decision(paths, command_scope, kind, turn_id, **kwargs)

    monkeypatch.setattr(control, "_control_command_decision", decide)
    with _no_resource_growth(), ThreadPoolExecutor(max_workers=1) as pool:
        pending_stop = pool.submit(_stop_entry, lane, record, scope, entry)
        try:
            assert selected.wait(5)
            assert worker._requeue_owner_capacity(lane.paths, claim.path, payload)
            drain(lane, key)
            assert exact not in worker._CLAIM_EPOCHS
            worker.admission.release(user, conversation_key=key)
            pending_path = lane.paths.inbox / claim.name
            before = pending_path.read_bytes()
        finally:
            resume.set()
        assert pending_stop.result(timeout=5) is False
        assert pending_path.read_bytes() == before
        assert not read_json_file(pending_path).get("cancel_requested")
        assert not (lane.paths.terminal / claim.name).exists()
        stopped = claim.request_id in lane.scheduler.sequencer._box(key).stopped
        submitted = []
        counts = [worker.dispatch_pending_requests(
            lane.paths, worker.AdmissionLimits(5, 5), lambda *args: submitted.append(args),
        ) for _ in range(3)]
        assert (stopped, counts) == (False, [1, 0, 0])
        assert len(submitted) == 1
        assert submitted[0][0].request_id == claim.request_id
        assert submitted[0][0].execution_attempt_id != claim.execution_attempt_id


@pytest.mark.parametrize("lane", [4], indirect=True)
def test_fresh_stop_binding_read_has_no_stop_side_effect(lane):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    active = lane.scheduler.sequencer.snapshot(key)["active"]
    with _no_resource_growth():
        assert control._fresh_stop_runtime_binding(
            control._GatewayRequestRecord(claim.path, payload), _control_scope(payload),
        ) == {}
    assert claim.path.read_bytes() == before
    assert lane.scheduler.sequencer.snapshot(key)["active"] == active
    assert lane.scheduler.sequencer.snapshot(key)["state"] == "running"
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert exact in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_accepted_stop_publishes_only_after_durable_cancel(lane, monkeypatch, entry):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    original_write = control.update_json_file_atomic
    observed = []

    def write(path, updater, **kwargs):
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
        result = original_write(path, updater, **kwargs)
        observed.append(read_json_file(path)["cancel_requested"])
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
        return result

    monkeypatch.setattr(control, "update_json_file_atomic", write)
    with _no_resource_growth():
        assert _stop_entry(lane, control._GatewayRequestRecord(claim.path, payload), _control_scope(payload), entry)
    assert observed == [True]
    assert read_json_file(claim.path)["cancel_requested"] is True
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped
    assert lane.scheduler.sequencer.decide(actor.Admit(user, key, claim.request_id, "later", 7)).action == "rejected"
    assert exact in worker._CLAIM_EPOCHS
    with _no_resource_growth():
        finish(lane, claim, {"id": claim.request_id, "status": "interrupted", "ok": False})
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("outcome", ["requeued", "failed"])
@pytest.mark.parametrize("stopped", [False, True])
def test_verified_stale_processing_recovery_handoff(lane, outcome, stopped):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    _, key, exact, other = bind(lane, claim)
    if stopped:
        lane.scheduler.sequencer.decide(actor.Stop(user, key, claim.request_id))
    context = recovery._RecoveryContext(
        worker.time.time() + 1000, 2 if outcome == "requeued" else 1, 1, False, None, 1,
    )
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, params=context)
        assert report.load_errors == []
        assert report.summary[outcome] == 1
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS
    assert not claim.path.exists()
    if outcome == "requeued":
        assert read_json_file(lane.paths.inbox / claim.name)["status"] == "pending"
        assert (claim.request_id in lane.scheduler.sequencer._box(key).stopped) is stopped
        assert not (lane.paths.terminal / claim.name).exists()
    else:
        assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "failed"
        assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped


@pytest.mark.parametrize("lane", [4], indirect=True)
def test_unproven_recovery_source_retains_actor_and_claim(lane):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_processing_recovery import (
        recover_stale_processing,
    )

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    before = claim.path.read_bytes()
    context = recovery._RecoveryContext(worker.time.time(), 2, 1, False, lane.agent, 1, admission_gate=object())
    with _no_resource_growth(), pytest.raises(RequestExecutionOutcomeUncertainError):
        recover_stale_processing(lane.paths, claim.path, payload, context, recovery._settle_stale_processing)
    assert claim.path.read_bytes() == before
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert exact in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("target", ["new_attempt", "new_epoch", "other_request"])
def test_terminal_recovery_keeps_a_newer_active_tuple(lane, target):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, other = bind(lane, claim)
    recovery.write_json_file_atomic(claim.path, recovery._terminal_gateway_request_payload(
        claim.request_id, payload, {"id": claim.request_id, "status": "done", "ok": True},
    ))
    targets = {
        "new_attempt": (claim.request_id, "later-attempt", 7),
        "new_epoch": (claim.request_id, claim.execution_attempt_id, 8),
        "other_request": ("other-request", "other-attempt", 7),
    }
    current = targets[target]
    assert lane.scheduler.sequencer.decide(actor.Recover(
        user, key, *current, exit_proof=True, previous_attempt=(claim.request_id, claim.execution_attempt_id, 7),
    )).action == "admit"
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, agent=None)
        assert report.load_errors == []
        assert report.summary["archived"] == 1
        drain(lane, key)
    assert lane.scheduler.sequencer.snapshot(key)["active"] == current
    assert exact not in worker._CLAIM_EPOCHS
    assert other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
@pytest.mark.parametrize("rejection", ["io", "new_attempt", "wrong_scope", "terminal"])
def test_rejected_stop_leaves_no_uncommitted_tombstone(lane, monkeypatch, entry, rejection):
    enqueue(lane)
    claim, user, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    record, scope = control._GatewayRequestRecord(claim.path, payload), _control_scope(payload)
    if rejection == "io":
        def unavailable(*_args, **_kwargs):
            raise OSError("simulated cancellation write I/O error")
        monkeypatch.setattr(control, "update_json_file_atomic", unavailable)
    elif rejection == "new_attempt":
        update_json_file_atomic(claim.path, lambda current: dict(
            current, execution_attempt_id="later-attempt", lease_epoch=claim.lease_epoch + 1,
        ))
        assert lane.scheduler.sequencer.decide(actor.Recover(
            user, key, claim.request_id, "later-attempt", 8, exit_proof=True,
            previous_attempt=(claim.request_id, claim.execution_attempt_id, 7),
        )).action == "admit"
    elif rejection == "wrong_scope":
        update_json_file_atomic(claim.path, lambda current: dict(current, user_id="another-owner"))
    else:
        finish(lane, claim, {"id": claim.request_id, "status": "done", "ok": True})
        drain(lane, key)
    selected_path = lane.paths.terminal / claim.name if rejection == "terminal" else claim.path
    before = selected_path.read_bytes()
    active = lane.scheduler.sequencer.snapshot(key)["active"]
    with _no_resource_growth():
        assert _stop_entry(lane, record, scope, entry) is False
    assert selected_path.read_bytes() == before
    assert not read_json_file(selected_path).get("cancel_requested")
    assert lane.scheduler.sequencer.snapshot(key)["active"] == active
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    assert (exact in worker._CLAIM_EPOCHS) is (rejection != "terminal")


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("entry", ["mark", "live_window"])
def test_committed_stop_contention_keeps_durable_cancel_authoritative(lane, monkeypatch, entry):
    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    held, release = threading.Event(), threading.Event()
    original_write = control.update_json_file_atomic
    original_decision = control._control_command_decision
    memory_results = []

    def decide(paths, scope, kind, request_id, **kwargs):
        result = original_decision(paths, scope, kind, request_id, **kwargs)
        if kind == "stop_committed":
            memory_results.append(result[2].action)
        return result

    def hold():
        with lane.scheduler.sequencer._lock:
            held.set()
            assert release.wait(5)

    with _no_resource_growth(), ThreadPoolExecutor(max_workers=1) as pool:
        waiting = []

        def write(path, updater, **kwargs):
            result = original_write(path, updater, **kwargs)
            waiting.append(pool.submit(hold))
            assert held.wait(5)
            return result

        monkeypatch.setattr(control, "update_json_file_atomic", write)
        monkeypatch.setattr(control, "_control_command_decision", decide)
        try:
            assert _stop_entry(lane, control._GatewayRequestRecord(claim.path, payload), _control_scope(payload), entry)
            assert read_json_file(claim.path)["cancel_requested"] is True
        finally:
            release.set()
        for future in waiting:
            future.result(timeout=5)
    assert memory_results == ["retry"]
    assert exact in worker._CLAIM_EPOCHS
    assert claim.request_id not in lane.scheduler.sequencer._box(key).stopped
    handled = []

    def interrupted(*_args, **kwargs):
        handled.append(kwargs["refresh_lease"])
        return {"id": claim.request_id, "status": "interrupted", "ok": False}

    monkeypatch.setattr(worker, "_handle_gateway_request", interrupted)
    with _no_resource_growth():
        assert worker._process_claimed_gateway_request_path(
            lane.agent, lane.paths, claim.path, "worker", claim=claim,
        )
        drain(lane, key)
    assert handled == [False]
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
    assert read_json_file(lane.paths.terminal / claim.name)["terminal_response"]["status"] == "interrupted"


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("change", ["new_attempt", "new_epoch", "move_error"])
def test_unsuccessful_recovery_preserves_old_responsibility(lane, monkeypatch, capsys, change):
    from agent_py_agent.agent.gateway_parts.multiprocess_processing_recovery import (
        recover_stale_processing,
    )

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, other = bind(lane, claim)
    lane.scheduler.sequencer.decide(actor.Stop(worker.request_user_key(payload), key, claim.request_id))
    context = recovery._RecoveryContext(worker.time.time() + 1000, 2, 1, False, None, 1)
    errors = []
    if change == "new_attempt":
        update_json_file_atomic(claim.path, lambda current: dict(current, execution_attempt_id="replacement"))
    elif change == "new_epoch":
        update_json_file_atomic(claim.path, lambda current: dict(current, lease_epoch=claim.lease_epoch + 1))
    else:
        original_replace = Path.replace

        def move(path, target):
            if path == claim.path:
                error = OSError("simulated recovery move I/O error")
                errors.append(error)
                raise error
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", move)
    with _no_resource_growth():
        assert recover_stale_processing(
            lane.paths, claim.path, payload, context, recovery._settle_stale_processing,
        ) == ""
    if change == "move_error":
        assert len(errors) == 1 and errors[0].__cause__ is None
        stderr = capsys.readouterr().err
        assert '"operation": "requeue_gateway_request"' in stderr
        assert '"category": "io"' in stderr
        assert str(errors[0]) in stderr
    assert claim.path.exists()
    assert not (lane.paths.terminal / claim.name).exists()
    assert lane.scheduler.sequencer.snapshot(key)["active"] == (claim.request_id, claim.execution_attempt_id, 7)
    assert claim.request_id in lane.scheduler.sequencer._box(key).stopped
    assert exact in worker._CLAIM_EPOCHS and other in worker._CLAIM_EPOCHS


@pytest.mark.parametrize("lane", [4], indirect=True)
@pytest.mark.parametrize("outcome", ["archived", "failed", "requeued"])
def test_recovery_notifies_actor_after_original_transition_exit(lane, monkeypatch, outcome):
    from agent_py_agent.agent.gateway_parts import multiprocess_processing_recovery as metadata

    enqueue(lane)
    claim, _, _ = dispatch(lane)
    payload, key, exact, _ = bind(lane, claim)
    if outcome == "archived":
        recovery.write_json_file_atomic(claim.path, recovery._terminal_gateway_request_payload(
            claim.request_id, payload, {"id": claim.request_id, "status": "done", "ok": True},
        ))
    entered, notifications, trace = [], [], []
    original_scope = metadata.gateway_turn_transition
    original_post = lane.scheduler.post

    @contextmanager
    def scope(*args, **kwargs):
        acquired = False
        try:
            with original_scope(*args, **kwargs):
                acquired = True
                entered.append(True)
                trace.append("enter")
                yield
        finally:
            if acquired:
                entered.pop()
                trace.append("exit")

    def post(command, **kwargs):
        assert not entered, "recovery must release T before notifying the actor"
        if command.request_id == claim.request_id:
            assert trace and trace[-1] == "exit", "the real recovery transition must have been observed"
            trace.append("notify")
            notifications.append(command)
        return original_post(command, **kwargs)

    monkeypatch.setattr(metadata, "gateway_turn_transition", scope)
    monkeypatch.setattr(lane.scheduler, "post", post)
    context = recovery._RecoveryContext(
        worker.time.time() + 1000, 2 if outcome == "requeued" else 1, 1, False, None, 1,
    )
    with _no_resource_growth():
        report = recovery.recover_gateway_processing_requests_report(lane.paths, params=context)
        assert report.load_errors == []
        assert report.summary[outcome] == 1
        drain(lane, key)
    assert len(notifications) == 1
    assert trace.count("enter") == trace.count("exit") > 0
    assert trace[trace.index("notify") - 1] == "exit"
    assert lane.scheduler.sequencer.snapshot(key)["active"] is None
    assert exact not in worker._CLAIM_EPOCHS
