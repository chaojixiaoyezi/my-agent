"""R3-R1：默认超时的真实业务取消，以及原模型 worker 的保留权交接。"""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallLedger,
    ModelCallStartedParams,
    model_call_drain_snapshot,
)
from agent_py_agent.agent.conversation.background_team_lifecycle import (
    identity_outstanding,
    reconcile_background_identity,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_mega_background_lifecycle_controls import (
    native_cancellation_case,
    ready_index,
)


def _started_call(ledger, call_id, *, run_id="original-run", attempt_id="original-attempt"):
    ledger.started(ModelCallStartedParams(call_id=call_id, backend="controlled", model="fixture",
        input_tokens=1, output_tokens_estimate=1, run_id=run_id, metadata={"attempt_id": attempt_id}))


def _retentions(ledger, call_id):
    return [token for source, row, tokens in model_call_drain_snapshot()
            if source is ledger and row.call_id == call_id for token in tokens]


def _facts(agent, backend, future, task_id):
    task = agent.conversation_store.session_tasks.require(task_id)
    claim = agent.conversation_store.claims.load(task.target_thread_id)
    ref = claim["runtime_authority"]
    repo = agent.subagents.runtime_db
    return {"timeout": agent.config.request_timeout, "worker_alive": backend.waiting_worker.is_alive(),
        "scheduler_done": future.done(), "claim_id": claim["claim_id"], "claim_status": claim["status"],
        "run_id": ref["run_id"], "attempt_id": ref["attempt_id"],
        "attempt_status": repo.get_attempt(ref["attempt_id"])["status"],
        "execution_lock": repo.get_agent_run_exec_lock(ref["agent_run_id"]),
        "worker_exit_unknown": task.metadata.get("cancellation_control", {}).get("model_worker_exit_unknown", False),
        "retained_original": any(row.run_id == ref["run_id"] and row.metadata.get("attempt_id") == ref["attempt_id"]
                                 for _ledger, row, _tokens in model_call_drain_snapshot())}


@pytest.mark.parametrize("execution_only", [False, True])
def test_default_timeout_cancellation_waits_for_worker_then_retires(tmp_path, execution_only):
    timeout = AgentConfig().request_timeout
    assert timeout == 240
    agent, backend, scheduler, call, task_id, view = native_cancellation_case(tmp_path, request_timeout=timeout)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    foreign = ModelCallLedger()
    foreign_tokens = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            task = store.session_tasks.require(task_id)
            ref = store.claims.load(task.target_thread_id)["runtime_authority"]
            # 同 run 的另一 attempt、同 attempt 的另一 run 都不能阻塞本次准确核收。
            for index, (run, attempt) in enumerate(((ref["run_id"], "other-attempt"), ("other-run", ref["attempt_id"]))):
                call_id = f"foreign-{index}"
                _started_call(foreign, call_id, run_id=run, attempt_id=attempt)
                foreign_tokens.append(foreign.retain_call(call_id))
                foreign.failed(ModelCallFailureParams(call_id=call_id, error_type="logical_terminal"))
            action = "executions.cancel" if execution_only else "tasks.cancel"
            params = ({"run_id": ref["run_id"], "attempt_id": ref["attempt_id"], "include_descendants": True}
                      if execution_only else {"task_id": task_id, "include_descendants": True})
            assert call(action, params)["state"] == "pending"
            future.result(timeout=15)
            live = _facts(agent, backend, future, task_id)
            print("cancelled-worker-live", live)
            assert live["worker_alive"] and live["scheduler_done"]
            assert live["claim_status"] == "cancelled" and live["attempt_status"] == "cancelled"
            assert live["execution_lock"] is None
            assert call(action, params)["state"] == "pending"
            if not execution_only:
                ready_index(store)
                retiring = call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
                    expected_revision=view["revision"], idempotency_key="default-timeout-retire")
                assert retiring["agent"]["identity_state"] == "retiring"
            backend.release.set()
            backend.waiting_worker.join(timeout=15)
            assert not backend.waiting_worker.is_alive() and backend.returned.is_set()
            exited = _facts(agent, backend, future, task_id)
            print("original-worker-joined", exited)
            assert call(action, params)["state"] == "completed"
            assert foreign_tokens and all(_retentions(foreign, f"foreign-{index}") for index in range(2))
            if execution_only:
                assert call("tasks.cancel", {"task_id": task_id, "include_descendants": True})["state"] == "completed"
                pending = [wake for wake in store.wakes.pending(limit=0) if wake.thread_id == task.target_thread_id]
                assert len(pending) == 1
                wake = pending[0]
                assert wake.reason == "session_task" and wake.metadata["session_task_id"] == task_id
                assert wake.dedupe_key == f"session_task:{task_id}:dispatch"
                state, error = store.wakes.attempts.state_report(wake.wake_signal_id)
                assert error is None
                due = max(time.time(), state.next_attempt_at, scheduler._wake_retry_after.get(wake.wake_signal_id, 0))
                print("original-dispatch-wake-backoff", state.to_dict())
                # 原派活 wake 在 execution 取消后仍处退避；沿公开时钟推进到真实到期点，由原调度器核收。
                scheduler.tick_thread(task.target_thread_id, now=due)
                assert not any(row.wake_signal_id == wake.wake_signal_id for row in store.wakes.pending(limit=0))
                assert backend.calls == 2
                ready_index(store)
                call("background_agents.retire", {"agent_id": view["agent_id"], "disposition": "drain"},
                    expected_revision=view["revision"], idempotency_key="default-timeout-retire")
            for _ in range(3):
                retired = reconcile_background_identity(agent, view["agent_id"])
                if retired.metadata["background_agent"]["identity_state"] == "retired":
                    break
            assert retired.metadata["background_agent"]["identity_state"] == "retired", (
                identity_outstanding(agent, retired), store.session_tasks.require(task_id).metadata["cancellation_control"])
            assert not store.session_tasks.require(task_id).metadata["cancellation_control"]["outstanding_refs"]
            assert repo.get_agent_run_exec_lock(ref["agent_run_id"]) is None
            assert backend.calls == 2
            assert (agent.home_paths.owner_home_dir / "before-cancel-result.txt").read_text() == "harmless original result"
            assert not (agent.home_paths.owner_home_dir / "after-cancel-result.txt").exists()
        finally:
            backend.release.set()
            if backend.waiting_worker is not None:
                backend.waiting_worker.join(timeout=15)
            for token in foreign_tokens:
                token.release()
            future.result(timeout=15)


def _guard_state(ledger, call_id="guard-call"):
    return SimpleNamespace(ledger=ledger, call_id=call_id,
                           liveness=SimpleNamespace(abandon=lambda: None))


def _guard_request():
    return SimpleNamespace(agent=SimpleNamespace(config=AgentConfig(), backend=SimpleNamespace()), prompt="fixture")


def test_logical_failure_and_caller_timeout_do_not_release_worker_retention(monkeypatch):
    ledger = ModelCallLedger()
    _started_call(ledger, "guard-call")
    entered, release = Event(), Event()
    workers = []

    def backend(*_args):
        workers.append(current_thread())
        entered.set()
        assert release.wait(10)
        return "late"

    monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *_args: .01)
    monkeypatch.setattr(generation, "_generate_backend_response", backend)
    try:
        with pytest.raises(generation.ProviderTimeoutError):
            generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        assert entered.is_set() and workers[0].is_alive()
        ledger.failed(ModelCallFailureParams(call_id="guard-call", error_type="logical_failure"))
        assert _retentions(ledger, "guard-call"), "logical terminal cannot release the actual worker"
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=10)
    assert not _retentions(ledger, "guard-call")


@pytest.mark.parametrize("missing", ["ledger", "call", "retention"])
def test_missing_or_failed_exact_retention_prevents_backend_start(monkeypatch, missing):
    ledger = ModelCallLedger()
    if missing != "call":
        _started_call(ledger, "guard-call")
    calls = []
    monkeypatch.setattr(generation, "_generate_backend_response", lambda *_args: calls.append(True))
    if missing == "retention":
        def deny(_call_id):
            raise RuntimeError("retention unavailable")
        monkeypatch.setattr(ledger, "retain_call", deny)
    with pytest.raises((AttributeError, KeyError, RuntimeError, ValueError)):
        generation._generate_through_wall_guard(_guard_request(), _guard_state(None if missing == "ledger" else ledger))
    assert calls == []


@pytest.mark.parametrize("factory", ["Queue", "copy_context", "Thread"])
def test_failure_before_worker_construction_leaves_no_retention(monkeypatch, factory):
    ledger = ModelCallLedger()
    _started_call(ledger, "guard-call")

    def unavailable(**_kwargs):
        raise RuntimeError("thread construction failed before start")

    monkeypatch.setattr(generation, factory, unavailable)
    with pytest.raises(RuntimeError, match="construction failed"):
        generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
    assert not _retentions(ledger, "guard-call")


def test_start_ack_failure_keeps_retention_until_original_worker_cleanup(monkeypatch):
    from agent_py_agent.agent.conversation import background_shutdown

    ledger = ModelCallLedger()
    _started_call(ledger, "guard-call")
    entered, release = Event(), Event()
    workers = []

    def backend(*_args):
        entered.set()
        assert release.wait(10)
        return "late"

    def lose_ack(worker):
        workers.append(worker)
        worker.start()
        assert entered.wait(5)
        raise RuntimeError("worker started but startup acknowledgement lost")

    monkeypatch.setattr(generation, "_generate_backend_response", backend)
    monkeypatch.setattr(background_shutdown, "start_background_model_worker", lose_ack)
    try:
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        ledger.failed(ModelCallFailureParams(call_id="guard-call", error_type="logical_failure"))
        assert workers[0].is_alive()
        assert _retentions(ledger, "guard-call"), "startup error is not proof of no worker"
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=10)
    assert not _retentions(ledger, "guard-call")


def test_worker_retention_includes_final_interrupt_cleanup(monkeypatch):
    ledger = ModelCallLedger()
    _started_call(ledger, "guard-call")
    entered_cleanup, release_cleanup = Event(), Event()
    workers = []
    original_cleanup = generation.set_interrupt

    def backend(*_args):
        workers.append(current_thread())
        return "result"

    def cleanup(value):
        entered_cleanup.set()
        assert release_cleanup.wait(10)
        return original_cleanup(value)

    monkeypatch.setattr(generation, "_generate_backend_response", backend)
    monkeypatch.setattr(generation, "set_interrupt", cleanup)
    try:
        assert generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger)) == "result"
        assert entered_cleanup.wait(5)
        ledger.failed(ModelCallFailureParams(call_id="guard-call", error_type="logical_terminal"))
        assert workers[0].is_alive() and _retentions(ledger, "guard-call")
    finally:
        release_cleanup.set()
        for worker in workers:
            worker.join(timeout=10)
    assert not _retentions(ledger, "guard-call")


def test_zero_timeout_direct_call_keeps_existing_untracked_compatibility(monkeypatch):
    monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *_args: 0)
    calls = []
    monkeypatch.setattr(generation, "_invoke_backend_generate", lambda *_args: calls.append("direct") or "done")
    assert generation._generate_through_wall_guard(_guard_request(), _guard_state(None)) == "done"
    assert calls == ["direct"]


def test_untracked_legacy_marker_is_not_cleared_by_empty_current_ledger(tmp_path):
    agent, backend, scheduler, call, task_id, _view = native_cancellation_case(
        tmp_path, request_timeout=AgentConfig().request_timeout)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scheduler.tick)
        try:
            assert backend.waiting.wait(15)
            task = store.session_tasks.require(task_id)
            binding = store.claims.load(task.target_thread_id)["runtime_authority"]
            params = {"task_id": task_id, "include_descendants": True}
            assert call("tasks.cancel", params)["state"] == "pending"
            future.result(timeout=15)
            backend.release.set()
            backend.waiting_worker.join(timeout=15)
            assert not backend.waiting_worker.is_alive()
            assert not any(row.run_id == binding["run_id"] and row.metadata.get("attempt_id") == binding["attempt_id"]
                           for _ledger, row, _tokens in model_call_drain_snapshot())
            ref = repo.parent_execution_ref(owner_id=str(agent.home_paths.owner_id), run_id=binding["run_id"],
                                           attempt_id=binding["attempt_id"])
            # 模拟无原worker/进程身份的旧canonical记录；外部测试持有的Thread不能替它补签证据。
            store.session_tasks.record_execution_cancellation_control(task_id, execution_ref=ref,
                outstanding_refs=[ref.run_id], model_worker_exit_unknown=True)
            current = store.session_tasks.record_execution_cancellation_control(task_id, execution_ref=ref,
                outstanding_refs=[], model_worker_exit_unknown=False)
            assert current.metadata["cancellation_control"]["model_worker_exit_unknown"] is True
            assert call("tasks.cancel", params)["state"] == "pending"
            with pytest.raises(ValueError, match="MODEL_WORKER_EXIT_UNCONFIRMED"):
                store.session_tasks.record_cancellation_control(task_id, outstanding_refs=[], execution_ref=ref,
                    confirmed_refs=(ref.run_id, task_id, current.conversation_request_id),
                    expected_resource_refs=current.metadata["cancellation_control"]["resource_refs"])
        finally:
            backend.release.set()
            if backend.waiting_worker is not None:
                backend.waiting_worker.join(timeout=15)
            future.result(timeout=15)
