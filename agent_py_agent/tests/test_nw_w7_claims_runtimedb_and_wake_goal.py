"""W7 回归单测：覆盖 #28–#33 租约心跳/获取、RuntimeDB 重开/并发闭环、批次缓存隔离与目标续跑韧性。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderQueueTimeoutError,
    ProviderStreamIncompleteError,
    ProviderTransientError,
)
from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interruptible,
)
from agent_py_agent.agent.conversation import FakeDeliveryService
from agent_py_agent.agent.conversation.background_goal import (
    GoalContinuationDependencies,
    continue_goal_after_report,
)
from agent_py_agent.agent.conversation.goal_clock import GoalClockGroup
from agent_py_agent.agent.conversation.models import (
    BackgroundMainAgentReport,
    ThreadGoal,
    WakeSignal,
)
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunClaimHeartbeat,
    conversation_claim_interrupt_name,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    _complete_wake_report,
    _handle_nonquota_wake_error,
    _select_same_reason_wake_batch,
    _select_successful_completion_batch,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.task_run_closeout import settle_terminal_task_run
from agent_py_agent.agent.conversation.technical_continuation import (
    WAKE_REASON,
    claim_wake,
    receipt_for,
    record_closeout,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.run_creation import RunCreation, create_run_chain
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import EffectResolverPolicy, ToolRuntimePolicy


def _make_thread(store: ConversationStore, cid: str = "conv-w7"):
    return store.threads.get_or_create(
        {
            "canonical_user_id": "owner-w7",
            "channel": "internal",
            "channel_conversation_id": cid,
            "channel_user_id": "owner-w7",
            "now": 10.0,
        }
    )


def _closeout_events(repo: RuntimeRepository, task_run_id: str) -> list[dict]:
    with repo._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT event_type, payload_json FROM runtime_events "
            "WHERE task_run_id = ? ORDER BY created_at, event_id",
            (task_run_id,),
        ).fetchall()
    return [
        {"event_type": row["event_type"], "data": json.loads(row["payload_json"])}
        for row in rows
    ]


# ------------------------------------------------------------------ #28


def test_heartbeat_stop_before_start_and_post_stop_rearm_do_not_leak():
    renew_calls: list[dict] = []

    class _FakeClaims:
        def renew(self, req: dict) -> dict:
            renew_calls.append(dict(req))
            return {"claim_id": req["claim_id"]}

    hb = ConversationRunClaimHeartbeat(
        {
            "store": SimpleNamespace(claims=_FakeClaims()),
            "thread_id": "thread-stop-first",
            "claim_id": "claim-stop-first",
            "lease_seconds": 30,
            "interval_seconds": 0.05,
        }
    )
    hb.stop()
    assert not hb.is_alive()
    assert hb._pending is None
    assert hb._done.is_set()

    hb.start()
    assert not hb.is_alive()
    assert hb._pending is None
    time.sleep(0.12)
    assert renew_calls == []


def test_heartbeat_concurrent_stop_during_in_flight_renew_prevents_rearm():
    entered, release = threading.Event(), threading.Event()
    start_calls: list[str] = []

    class _SlowClaims:
        def renew(self, req: dict) -> dict:
            entered.set()
            release.wait(3.0)
            return {"claim_id": req["claim_id"]}

    hb = ConversationRunClaimHeartbeat(
        {
            "store": SimpleNamespace(claims=_SlowClaims()),
            "thread_id": "thread-race",
            "claim_id": "claim-race",
            "lease_seconds": 30,
            "interval_seconds": 0.05,
        }
    )
    orig_start = hb.start

    def tracked_start() -> None:
        start_calls.append("start")
        orig_start()

    hb.start = tracked_start  # type: ignore[method-assign]
    hb.start()
    assert entered.wait(3.0)
    stopper = threading.Thread(target=hb.stop, daemon=True)
    stopper.start()
    time.sleep(0.05)
    assert stopper.is_alive()
    release.set()
    stopper.join(3.0)
    hb.join(3.0)
    assert not stopper.is_alive()
    assert not hb.is_alive()
    assert start_calls == ["start"]


@pytest.mark.parametrize(
    "bad_owner",
    [
        {},
        None,
        "invalid-string",
        ["list"],
        {"pid": "not-an-int"},
        {"host_id": "host-only", "pid": 123},
        {"host_id": "host-only", "pid": True, "start_time": 1},
    ],
)
def test_claim_acquire_handles_malformed_owner_process_without_crashing(tmp_path: Path, bad_owner):
    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-malformed")
    first = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "test",
            "lease_seconds": 30,
            "now": 100.0,
        }
    )
    assert first is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    record = json.loads(claim_path.read_text(encoding="utf-8"))
    record["owner_process"] = bad_owner
    claim_path.write_text(json.dumps(record), encoding="utf-8")

    # Unexpired claim waits cleanly without raising KeyError/TypeError
    assert (
        store.claims.acquire(
            {
                "thread_id": thread.thread_id,
                "task_id": "task-2",
                "reason": "test",
                "now": 110.0,
            }
        )
        is None
    )
    # Expired claim with malformed owner_process reclaims cleanly
    recovered = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-2",
            "reason": "test",
            "now": 200.0,
        }
    )
    assert recovered is not None
    assert recovered["claim_id"] != first["claim_id"]
    assert recovered["acquisition"]["reason"] == "lease_expired"


def test_stale_finished_claim_handles_malformed_and_overflow_finished_at(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-overflow")
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": "t1", "now": 10.0})
    assert claim is not None
    finished = store.claims.finish(
        {"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "finished", "now": 20.0}
    )
    assert finished is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    for bad_finished_at in ("not-a-float", 1e309, "1e500"):
        raw = dict(finished, finished_at=bad_finished_at)
        claim_path.write_text(json.dumps(raw), encoding="utf-8")
        assert store.claims.archive_stale(current=1000.0, retention_seconds=60.0) == 0


def test_claim_acquire_blocks_stealing_expired_live_body_and_allows_exited_body(
    tmp_path: Path, monkeypatch
):
    from agent_py_agent.agent.conversation import store_claims as store_claims_mod

    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-live-expired")
    claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-live",
            "reason": "thread_goal_continue",
            "lease_seconds": 1,
            "now": 100.0,
        }
    )
    assert claim is not None

    # 1. In-process live body holding register_interruptible(conversation-claim:<id>)
    with register_interruptible(conversation_claim_interrupt_name(claim["claim_id"])):
        assert (
            store.claims.acquire(
                {
                    "thread_id": thread.thread_id,
                    "task_id": "task-competitor",
                    "reason": "test",
                    "now": 500.0,
                }
            )
            is None
        )

    # 2. Exited body whose finish failed settles cleanly after register_interruptible exits
    taken = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-recover",
            "reason": "test",
            "now": 500.0,
        }
    )
    assert taken is not None and taken["claim_id"] != claim["claim_id"]

    # 3. Foreign live PID with active/no settled runtime_authority blocks takeover;
    #    once RuntimeDB proves that attempt settled & unlocked, takeover succeeds cleanly.
    repo = RuntimeRepository(runtime_db_path(tmp_path))
    created = repo.record_run_creation(
        owner_id="owner-w7",
        conversation_task_id="task-live",
        run_id="run-live",
        attempt_status="pending",
    )
    claimed_attempt = repo.create_attempt(created["agent_run_id"], reuse_pending=True)

    claim2 = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-live",
            "reason": "thread_goal_continue",
            "lease_seconds": 1,
            "now": 600.0,
        }
    )
    assert claim2 is not None
    claim_path = store.storage.background_claims_dir / f"{thread.thread_id}.json"
    persisted = json.loads(claim_path.read_text(encoding="utf-8"))
    persisted["owner_process"] = {"host_id": "remote-test-host", "pid": 999888, "start_time": 42}
    persisted["runtime_authority"] = {
        "schema_version": "conversation_claim_execution.v1",
        "claim_id": claim2["claim_id"],
        "invocation_run_id": "inv-1",
        "task_id": "task-live",
        "run_id": "run-live",
        "agent_run_id": created["agent_run_id"],
        "attempt_id": claimed_attempt["attempt_id"],
    }
    claim_path.write_text(json.dumps(persisted), encoding="utf-8")

    monkeypatch.setattr(store_claims_mod, "process_identity_is_live", lambda _ident: True)
    assert (
        store.claims.acquire(
            {"thread_id": thread.thread_id, "task_id": "task-other", "reason": "test", "now": 900.0}
        )
        is None
    )

    repo.settle_agent_run(
        agent_run_id=created["agent_run_id"],
        status="done",
        attempt_id=claimed_attempt["attempt_id"],
    )
    settled_recovery = store.claims.acquire(
        {"thread_id": thread.thread_id, "task_id": "task-other", "reason": "test", "now": 900.0}
    )
    assert settled_recovery is not None and settled_recovery["claim_id"] != claim2["claim_id"]


# ------------------------------------------------------------------ #29


def test_closed_task_run_reopens_on_followup_agent_run_child_chain_queue_and_reuse_pending(
    tmp_path: Path,
):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    created = repo.record_run_creation(
        owner_id="owner-w7",
        conversation_task_id="task-reopen",
        run_id="run-root",
        attempt_status="pending",
    )
    task_run_id = created["task_run_id"]
    root_agent_run_id = created["agent_run_id"]
    attempt_1 = repo.create_attempt(root_agent_run_id, reuse_pending=True)
    repo.settle_agent_run(
        agent_run_id=root_agent_run_id, status="done", attempt_id=attempt_1["attempt_id"]
    )
    settled = repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=task_run_id, task_id=created["task_id"], operator="test", reason="done"
    )
    assert settled["settled"] is True
    assert repo.get_task_run(task_run_id)["closed_at"] > 0

    # 1. create_agent_run on closed task_run_id reopens TaskRun
    followup = repo.create_agent_run(
        task_run_id=task_run_id,
        parent_agent_run_id=root_agent_run_id,
        role="worker",
        run_id="run-followup",
    )
    reopened_1 = repo.get_task_run(task_run_id)
    assert (reopened_1["status"], reopened_1["closed_at"]) == ("created", 0)
    followup_attempt = repo.create_attempt(followup["agent_run_id"])
    repo.settle_agent_run(
        agent_run_id=followup["agent_run_id"],
        status="done",
        attempt_id=followup_attempt["attempt_id"],
    )
    assert repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=task_run_id, task_id=created["task_id"], operator="test", reason="done"
    )["settled"] is True

    # 2. create_run_chain with parent_run_id referencing a closed TaskRun reopens TaskRun
    with repo.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        child = create_run_chain(
            conn,
            RunCreation(
                owner_id="owner-w7",
                conversation_task_id="task-reopen",
                run_id="run-child-chain",
                role="worker",
                attempt_status="pending",
                attempt_metadata={},
                parent_run_id="run-root",
            ),
            now=time.time(),
        )
    reopened_2 = repo.get_task_run(task_run_id)
    assert (reopened_2["status"], reopened_2["closed_at"]) == ("created", 0)
    child_attempt = repo.create_attempt(child["agent_run_id"], reuse_pending=True)
    repo.settle_agent_run(
        agent_run_id=child["agent_run_id"], status="done", attempt_id=child_attempt["attempt_id"]
    )
    assert repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=task_run_id, task_id=created["task_id"], operator="test", reason="done"
    )["settled"] is True

    # 3. queue_pending_attempt reopens closed TaskRun
    queued = repo.queue_pending_attempt(root_agent_run_id, source="external_input")
    reopened_3 = repo.get_task_run(task_run_id)
    assert (reopened_3["status"], reopened_3["closed_at"]) == ("created", 0)

    # 4. create_attempt(..., reuse_pending=True) also reopens a closed TaskRun row atomically
    with repo.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE task_runs SET status = 'done', closed_at = 123.0 WHERE task_run_id = ?",
            (task_run_id,),
        )
    activated = repo.create_attempt(
        root_agent_run_id,
        reuse_pending=True,
        expected_pending_attempt_id=queued["attempt_id"],
    )
    assert activated["attempt_id"] == queued["attempt_id"]
    reopened_4 = repo.get_task_run(task_run_id)
    assert (reopened_4["status"], reopened_4["closed_at"]) == ("created", 0)
    events = _closeout_events(repo, task_run_id)
    assert sum(1 for item in events if item["event_type"] == "task_run.reopened") >= 4


# ------------------------------------------------------------------ #30


def test_closeout_enters_begin_immediate_before_eligibility_select(tmp_path: Path):
    db_path = tmp_path / "runtime.db"
    repo = RuntimeRepository(db_path)
    created = repo.record_run_creation(
        owner_id="owner-w7",
        conversation_task_id="task-lock",
        run_id="run-main",
        attempt_status="pending",
    )
    attempt_1 = repo.create_attempt(created["agent_run_id"], reuse_pending=True)
    repo.settle_agent_run(
        agent_run_id=created["agent_run_id"], status="done", attempt_id=attempt_1["attempt_id"]
    )

    # Hold an external BEGIN IMMEDIATE writer, start settle_task_run_if_agent_tree_terminal on another thread,
    # add a running child inside the writer before committing, and verify closeout rejects closing the TaskRun.
    external = sqlite3.connect(str(db_path), timeout=10.0)
    external.isolation_level = None
    external.execute("BEGIN IMMEDIATE")
    results: list[dict] = []
    started = threading.Event()

    def closeout_worker():
        started.set()
        results.append(
            repo.settle_task_run_if_agent_tree_terminal(
                task_run_id=created["task_run_id"],
                task_id=created["task_id"],
                operator="test",
                reason="race",
            )
        )

    worker = threading.Thread(target=closeout_worker, daemon=True)
    worker.start()
    try:
        assert started.wait(2.0)
        time.sleep(0.08)
        # If closeout had NOT issued BEGIN IMMEDIATE before SELECT, it could read before waiting.
        # Here worker MUST still be blocked waiting on our write lock.
        assert worker.is_alive()
        now = time.time()
        external.execute(
            "INSERT INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id, delegation_id, "
            "run_id, role, status, current_attempt_id, current_attempt_generation, workspace_epoch, "
            "created_at, updated_at) VALUES('child-1', ?, ?, '', 'run-child', 'worker', 'created', "
            "'attempt-child', 1, 1, ?, ?)",
            (created["task_run_id"], created["agent_run_id"], now, now),
        )
        external.execute(
            "INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at) "
            "VALUES('attempt-child', 'child-1', 1, 'running', ?)",
            (now,),
        )
        external.execute("COMMIT")
        worker.join(5.0)
    finally:
        external.close()

    assert results and results[0] == {"settled": False, "reason": "agent_tree_active"}
    assert repo.get_task_run(created["task_run_id"])["closed_at"] == 0


def test_settle_terminal_task_run_closes_standalone_nolink_nonmain_run(tmp_path: Path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    store = ConversationStore(tmp_path / "conversations")
    created = repo.record_run_creation(
        owner_id="owner-w7",
        conversation_task_id="sched-run-1",
        run_id="sched-run-1",
        role="scheduled",
        attempt_status="pending",
    )
    attempt = repo.create_attempt(created["agent_run_id"], reuse_pending=True)
    repo.settle_agent_run(
        agent_run_id=created["agent_run_id"], status="done", attempt_id=attempt["attempt_id"]
    )
    assert repo.main_agent_run_for_task(created["task_id"]) is None
    assert store.tasks.load(created["task_id"]) is None

    # Passing task_id without run_id previously skipped non-'main' root runs
    settle_terminal_task_run(repo, store, task_id=created["task_id"])
    row = repo.get_task_run(created["task_run_id"])
    assert row is not None and row["closed_at"] > 0 and row["status"] == "done"


# ------------------------------------------------------------------ #31


def test_completion_and_same_reason_batches_exclude_cached_owner_delivery(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-batch")
    agent = SimpleNamespace(config=AgentConfig(background_context_max_total_tokens=8000))
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    cached_primary = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "root-task-1",
            "reason": "subagent_runner_finished",
            "source_agent_id": "child-a",
            "metadata": {
                "status": "DONE",
                "owner_delivery": {
                    "schema_version": "wake-owner-delivery.v2",
                    "reason": "subagent_runner_finished",
                    "task_id": "root-task-1",
                    "content": "frozen cached delivery",
                    "route_channel": "internal",
                    "route_target": "conv-batch",
                },
            },
        }
    )
    fresh_sibling = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "root-task-1",
            "reason": "subagent_runner_finished",
            "source_agent_id": "child-b",
            "metadata": {"status": "DONE"},
        }
    )

    batch_from_cached = _select_successful_completion_batch(
        scheduler, cached_primary, [cached_primary, fresh_sibling]
    )
    assert [w.wake_signal_id for w in batch_from_cached] == [cached_primary.wake_signal_id]

    batch_from_fresh = _select_successful_completion_batch(
        scheduler, fresh_sibling, [fresh_sibling, cached_primary]
    )
    assert [w.wake_signal_id for w in batch_from_fresh] == [fresh_sibling.wake_signal_id]

    cached_generic = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "root-task-2",
            "reason": "scheduled_job_due",
            "metadata": {
                "owner_delivery": {
                    "schema_version": "wake-owner-delivery.v2",
                    "reason": "scheduled_job_due",
                    "task_id": "root-task-2",
                    "content": "frozen generic delivery",
                    "route_channel": "internal",
                    "route_target": "conv-batch",
                }
            },
        }
    )
    fresh_generic = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "root-task-2",
            "reason": "scheduled_job_due",
            "metadata": {},
        }
    )
    assert [
        w.wake_signal_id
        for w in _select_same_reason_wake_batch(scheduler, cached_generic, [cached_generic, fresh_generic])
    ] == [cached_generic.wake_signal_id]
    assert [
        w.wake_signal_id
        for w in _select_same_reason_wake_batch(scheduler, fresh_generic, [fresh_generic, cached_generic])
    ] == [fresh_generic.wake_signal_id]


# ------------------------------------------------------------------ #32 & #33


def _goal_deps(
    store: ConversationStore,
    goal: ThreadGoal,
    *,
    phase_fn,
    raise_wake_fn,
    idle_limit: int = 3,
) -> GoalContinuationDependencies:
    return GoalContinuationDependencies(
        goals=store.goals,
        tasks=store.tasks,
        goal_clock=GoalClockGroup(),
        task_status=lambda _thread_id, _task_id: "active",
        subagent_phase=phase_fn,
        raise_wake=raise_wake_fn,
        task_registry=lambda: None,
        goal_continuation_idle_limit=idle_limit,
    )


def test_continue_goal_after_report_handles_state_error_and_retries_raise_wake(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-goal-w7")
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "goal-task-1", "goal": "keep moving"})
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "keep moving", "task_id": "goal-task-1"}
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "metadata": {"goal_id": goal.goal_id, "channel": "internal", "conversation_id": "conv-goal-w7"},
        }
    )
    report = BackgroundMainAgentReport(
        thread_id=thread.thread_id,
        task_id=goal.task_id,
        reason="thread_goal_continue",
        response="progress slice",
        route_channel="internal",
        route_target="conv-goal-w7",
        created_at=50.0,
        tool_call_count=1,
        tool_success_count=1,
        material_progress_count=1,
        goal_continuation_allowed=True,
    )

    raised_attempts: list[int] = []

    def flaky_wake(*_args, **_kwargs):
        raised_attempts.append(len(raised_attempts) + 1)
        if len(raised_attempts) == 1:
            raise OSError("transient wake queue lock error")
        return WakeSignal(
            wake_signal_id="w-retried",
            thread_id=thread.thread_id,
            root_task_id=goal.task_id,
            reason="thread_goal_continue",
        )

    # 1. state_error != "" still continues & raise_wake retries idempotently once on exception
    deps = _goal_deps(
        store,
        goal,
        phase_fn=lambda _tid: ("subagent_state_unknown", "subagent_state_load_error"),
        raise_wake_fn=flaky_wake,
    )
    continue_goal_after_report(deps, signal, report=report, now=50.0)
    assert raised_attempts == [1, 2]
    assert store.goals.load(thread.thread_id).status == "active"

    # 2. Confirmed subagents_active still returns early without charging idle streak or waking
    active_calls: list[int] = []
    deps_active = _goal_deps(
        store,
        goal,
        phase_fn=lambda _tid: ("subagents_active", ""),
        raise_wake_fn=lambda *a, **kw: active_calls.append(1),
    )
    continue_goal_after_report(deps_active, signal, report=report, now=60.0)
    assert active_calls == []


@pytest.mark.parametrize(
    "transient_exc",
    [
        ProviderStreamIncompleteError("upstream stream truncated before finish"),
        ProviderQueueTimeoutError("local queue wait expired"),
        ProviderTransientError("HTTP 503: upstream busy"),
    ],
)
def test_nonquota_wake_error_preserves_goal_and_wake_on_transient_provider_errors(
    tmp_path: Path, transient_exc: BaseException
):
    store = ConversationStore(tmp_path / "conversations")
    thread = _make_thread(store, "conv-transient")
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "goal-task-2", "goal": "survive"})
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "survive", "task_id": "goal-task-2"}
    )
    signal = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "metadata": {"goal_id": goal.goal_id},
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=SimpleNamespace(config=AgentConfig()), store=store, channels=FakeDeliveryService()
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    released: list[object] = []
    finished: list[object] = []
    scheduler.scheduler_service = SimpleNamespace(
        release=lambda c, **kw: released.append(c),
        finish=lambda c, **kw: finished.append(kw),
    )
    claim = object()

    _handle_nonquota_wake_error(
        scheduler,
        signal,
        lifecycle_reason="thread_goal_continue",
        claim=claim,
        error=transient_exc,
    )

    assert released == [claim]
    assert finished == []
    assert store.goals.load(thread.thread_id).status == "active"
    pending_ids = {w.wake_signal_id for w in store.wakes.pending()}
    assert signal.wake_signal_id in pending_ids


def test_stopped_technical_cutoff_no_longer_suppresses_goal_continuation(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="owner-w7", owner_id="owner-w7")
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "finish goal",
            "metadata": {"conversation_request_id": "req-1"},
        }
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "goal-cutoff-1", "goal": "finish goal"})
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "finish goal", "task_id": "goal-cutoff-1"}
    )
    policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect="mutating"))
    tools = SimpleNamespace(
        runtime_snapshot=lambda: SimpleNamespace(
            runtime=lambda _name: SimpleNamespace(runtime_policy=policy)
        )
    )
    agent = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_id="owner-w7"),
        config=AgentConfig(
            technical_cutoff_continuation_enabled=True,
            goal_continuation_idle_limit=3,
        ),
        tools=tools,
    )
    # Drive cutoff receipt into terminal "stopped" state (no progress on continuation slice)
    ctx1 = SimpleNamespace(
        do_save=True,
        context_scope="conversation",
        source="gateway",
        request_id="req-1",
        run_id="run-1",
        attempt_id="attempt-1",
        task_attributes={"conversation_task_id": "goal-cutoff-1", "conversation_thread_id": thread.thread_id},
        archive_tool_calls=[{"tool": "write", "ok": True, "handler_executed": True}],
        final_response=SimpleNamespace(
            runtime_reason="TOOL_ROUND_LIMIT_REACHED",
            runtime_source="tool_loop",
            runtime_status="unfinished",
        ),
    )
    record_closeout(agent, ctx1)
    cutoff_wake = next(w for w in store.wakes.pending(limit=20) if w.reason == WAKE_REASON)
    assert claim_wake(agent, store, cutoff_wake)

    ctx2 = SimpleNamespace(
        **{
            **vars(ctx1),
            "source": "background_main_agent",
            "do_save": False,
            "attempt_id": "attempt-2",
            "archive_tool_calls": [],
        }
    )
    stopped_receipt = record_closeout(agent, ctx2)
    assert stopped_receipt is not None and stopped_receipt.status == "stopped"
    assert receipt_for(store.tasks.load("goal-cutoff-1")).status == "stopped"

    # Now record a wake outcome after cutoff became "stopped": Goal continuation must still raise its wake.
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    goal_wake = store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "metadata": {"goal_id": goal.goal_id},
        }
    )
    report = BackgroundMainAgentReport(
        thread_id=thread.thread_id,
        task_id=goal.task_id,
        reason="thread_goal_continue",
        response="progress slice",
        route_channel="internal",
        route_target=thread.thread_id,
        created_at=time.time(),
        tool_call_count=1,
        tool_success_count=1,
        material_progress_count=1,
        wake_handled=True,
        goal_continuation_allowed=True,
    )
    _complete_wake_report(
        scheduler,
        goal_wake,
        report,
        lifecycle_reason="thread_goal_continue",
        now=time.time(),
    )

    pending_reasons = [w.reason for w in store.wakes.pending(limit=20)]
    assert "thread_goal_continue" in pending_reasons
