"""真实队列交接只释放已完成的精确 actor 身份，不把让出执行当成终态。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import conversation_actor as actor
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
