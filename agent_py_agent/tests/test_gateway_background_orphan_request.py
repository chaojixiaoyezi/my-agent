"""原canonical child事实与C metadata生产入口，不用locator或模型正文替代工作身份。"""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    update_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.request_errors import RequestExecutionOutcomeUncertainError
from agent_py_agent.agent.gateway_parts.request_worker import (
    terminalize_unhandled_claimed_gateway_request,
)
from agent_py_agent.cli import gateway_loops
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)
from agent_py_agent.tests.test_gateway_orphan_reconciler import _scoped_restart_fixture


def test_original_canonical_orphan_has_production_source_without_c_owner_agent_build(tmp_path):
    base, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = scoped.subagents.create_run(goal="原孤儿合同", thought="实际待派工作", plan=["运行"], depth=1)
    discovery = SimpleNamespace(control=SimpleNamespace(home_root=base.home_paths.root,
        config=replace(base.config, memory_curator_enabled=False, enable_self_learning=False)), _source_cursors={})
    refs = list(CoordinatorBackgroundRequests._owner_sources(discovery,
        SimpleNamespace(identity=owner, home_dir=scoped.home_paths.owner_home_dir)))
    assert any(ref["kind"] == "orphan" and ref["source_id"] == task.id for ref in refs), "canonical_child_must_enter_original_p3_p5_transport"


def test_positive_mp_c_orphan_tick_never_constructs_remote_owner_agent(tmp_path, monkeypatch):
    base, owner, _ = _scoped_restart_fixture(tmp_path)
    context = SimpleNamespace(agent=base, config_path=tmp_path / "agent.yaml", control_plane=object())
    reconciler = gateway_loops._GatewayOrphanReconciler(context)
    reconciler._registry.record(owner)
    monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)
    monkeypatch.setattr(reconciler, "_sweep", lambda *args: {})
    def forbid_owner_pool():
        raise AssertionError("original_C_remote_pool_get_is_not_P5")
    monkeypatch.setattr(reconciler, "_ensure_owner_pool", forbid_owner_pool)
    reconciler.tick()


def test_independent_original_child_request_p3_p5_consumes_original_runner_and_model(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        request = json.loads(claim.path.read_text())
        run_id = request["metadata"]["background_source"]["source_id"]
        before = agent.subagents.load(run_id)
        backend.release.set()
        response = run_original(agent, gate, claim)
        assert response["ok"], response
        assert backend.calls == 1 and response["background_result"]["run_id"] == run_id
        after = agent.subagents.load(run_id)
        assert (after.id, after.root_id, after.parent_id, after.owner) == (before.id, before.root_id, before.parent_id, before.owner)
        assert after.runner_attempts == before.runner_attempts + 1


@pytest.mark.parametrize("fault", ["cancel", "generation"])
def test_child_network_wait_revocation_refuses_original_result_and_preserves_transport_p3(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, close):
        run_id = read_json_file(claim.path)["metadata"]["background_source"]["source_id"]
        p3 = reservation_file(gate.paths, claim.request_id)
        before_p3 = p3.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert backend.started.wait(5), future.result(1) if future.done() else "runner_still_inflight"
                running = agent.subagents.load(run_id)
                canonical = Path(running.agent_run_workspace_dir) / "canonical_state.json"
                before = canonical.read_bytes()
                assert running.status == "RUNNING" and running.runner_active_attempt_id
                with gateway_turn_transition(gate.paths, claim.request_id):
                    if fault == "cancel":
                        update_json_file_atomic(claim.path, lambda row: {**row, "cancel_requested": True, "turn_phase": "closing"})
                if fault == "generation":
                    close()
            finally:
                backend.release.set()
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                future.result(5)
        assert backend.calls == 1 and canonical.read_bytes() == before
        assert not Path(running.runner_result_json).exists()
        terminalize_unhandled_claimed_gateway_request(gate.paths, claim.path, caught.value, agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        assert p3.read_bytes() == before_p3 and not (gate.paths.terminal / claim.path.name).exists()
        assert read_json_file(claim.path)["execution_outcome"]["status"] == "unknown"


def test_captured_child_scope_after_original_request_body_exits_never_creates_late_write_or_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runner import worker
    from agent_py_agent.agent.gateway_parts.multiprocess_subagent_execution import (
        child_worker_scope,
    )

    captured = []
    original = worker._run_bound_subagent_worker
    def observe(actual_worker, params):
        captured.append((params.request_transport, actual_worker))
        return original(actual_worker, params)
    monkeypatch.setattr(worker, "_run_bound_subagent_worker", observe)
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        backend.release.set()
        assert run_original(agent, gate, claim)["ok"]
        [(transport, actual_worker)] = captured
        before = Path(transport.canonical_ref).read_bytes()
        entered = []
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            with child_worker_scope(transport, actual_worker):
                entered.append(True)
        assert not entered and Path(transport.canonical_ref).read_bytes() == before and backend.calls == 1


def test_mp_stalled_host_pid_without_original_native_actor_handle_is_unknown_and_never_killed(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
        _terminate_fully_stalled_source_hosts,
    )
    from agent_py_agent.agent.subagents import process_control

    monkeypatch.setattr(process_control, "terminate_pid_with_escalation", lambda *args, **kwargs: pytest.fail("bare_pid_is_not_native_actor"))
    with curator_case(tmp_path, monkeypatch, source_kind="orphan"):
        result = _terminate_fully_stalled_source_hosts({123456})
    assert result == {123456: {"status": "unknown", "pid": 123456, "reason_code": "native_original_actor_handle_required"}}


@pytest.mark.parametrize("identity_state", ["alive", "unknown"])
def test_mp_old_source_ttl_with_live_or_unknown_identity_never_grants_dead_or_child_start(tmp_path, monkeypatch, identity_state):
    import os
    import time

    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        capability_auto_sweep as sweep,
    )
    from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth

    session = {"worker_pid": os.getpid(), "status": "running", "heartbeat_at": time.time() - 3600,
        "process_epoch": "previous-process-epoch", "in_process": True}
    if identity_state == "alive":
        session["worker_process"] = {"host_id": process_host_id(), "pid": os.getpid(), "start_time": process_birth(os.getpid())}
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        pid, alive_or_unknown, cross_generation, proven_dead = sweep._runner_process_reclaim_facts(session)
        assert pid == os.getpid() and alive_or_unknown and not cross_generation and not proven_dead
        task = agent.subagents.load(read_json_file(claim.path)["metadata"]["background_source"]["source_id"])
        task.status = "RUNNING"
        task.attributes = {**task.attributes, "runner_session": session}
        assert sweep._dead_running_reclaim_facts(task, SimpleNamespace(allowed=True), set(), {}) is None
        assert not sweep._is_stalled_dispatchable_orphan(task, SimpleNamespace(allowed=True))
        assert backend.calls == 0


def test_timed_child_keeps_original_transport_active_until_actual_model_thread_exits(tmp_path, monkeypatch):
    import threading

    timed, completed = threading.Event(), threading.Event()
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        agent.config.runner_timeout_by_role = {"*": "1.0"}
        result_type = type(agent.subagents.runner_result)
        original = result_type.record_runner_result
        def observe(service, params):
            result = original(service, params)
            if params.status == "TIMEOUT":
                timed.set()
            return result
        monkeypatch.setattr(result_type, "record_runner_result", observe)
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            future.add_done_callback(lambda _done: completed.set())
            try:
                assert backend.started.wait(5), future.result(1) if future.done() else "child_not_yet_at_provider"
                assert timed.wait(5), "original_timeout_attempt_fence_must_really_run"
                assert not completed.wait(0.2), "actual_model_thread_is_still_inflight_not_a_drained_transport"
                with gateway_turn_transition(gate.paths, claim.request_id):
                    assert read_json_file(claim.path)["status"] == "processing"
                assert p3.read_bytes() == before and not (gate.paths.terminal / claim.path.name).exists()
            finally:
                backend.release.set()
            response = future.result(5)
        assert response["ok"] and backend.calls == 1
        assert response["background_result"]["status"] == "TIMEOUT"


def test_child_keeps_original_transport_active_until_actual_heartbeat_thread_exits(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.agent_core.runner import session_pool, worker

    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = session_pool._record_runner_session
    def observe(lease, session, *, status, error=None, require_persisted=False):
        if threading.current_thread().name.startswith("runner-session-") and not started.is_set():
            started.set()
            release.wait(5)
        return original(lease, session, status=status, error=error, require_persisted=require_persisted)
    monkeypatch.setattr(session_pool, "_record_runner_session", observe)
    monkeypatch.setattr(worker, "_RUNNER_SESSION_HEARTBEAT_SECONDS", 0.01)
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            future.add_done_callback(lambda _done: completed.set())
            try:
                assert backend.started.wait(5) and started.wait(5)
                backend.release.set()
                assert not completed.wait(2.0), "actual_session_thread_is_still_inflight_not_a_drained_transport"
                with gateway_turn_transition(gate.paths, claim.request_id):
                    assert read_json_file(claim.path)["status"] == "processing"
                assert p3.read_bytes() == before and not (gate.paths.terminal / claim.path.name).exists()
            finally:
                release.set()
                backend.release.set()
            response = future.result(5)
        assert response["ok"] and backend.calls == 1


def test_original_child_bounded_unknown_is_observed_before_child_scope_exit_and_call_reap(tmp_path, monkeypatch):
    from contextlib import ExitStack

    from agent_py_agent.agent.backends.bounded_call import (
        BoundedCallExitUnconfirmedError,
        bounded_call_error_origin,
    )
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        captured = []
        p3 = reservation_file(gate.paths, claim.request_id)
        before_p3 = p3.read_bytes()
        with ExitStack() as retained:
            # LLM: 只在真实供应商边界制造原Call物理尾线程；原runner/attempt/child scope和模型账本不替换，不签假call_id。
            # 函数用途: 让原异常穿过真实子代理出口，确认原位记账早于来源失效。
            def failed_backend(actual, prompt, **kwargs):
                actual.calls += 1
                actual.started.set()
                error, call = retained.enter_context(held_call(monkeypatch, key="actual-child-original-call"))
                run_id = read_json_file(claim.path)["metadata"]["background_source"]["source_id"]
                canonical = Path(agent.subagents.load(run_id).agent_run_workspace_dir) / "canonical_state.json"
                captured.append((error, call, canonical, canonical.read_bytes()))
                raise error

            monkeypatch.setattr(type(backend), "generate", failed_backend)
            caught, response = None, None
            try:
                response = run_original(agent, gate, claim)
            except BoundedCallExitUnconfirmedError as observed:
                caught = observed
            [(error, call, canonical, before)] = captured
            assert caught is error, {"response": response, "backend_calls": backend.calls}
            assert bounded_call_error_origin(error) is not None
            assert call.worker.is_alive() and call.retained() and backend.calls == 1
            pending = read_json_file(claim.path)
            assert pending["status"] == "processing"
            assert pending["execution_outcome"]["call_id"] == error.call_id
            assert pending["execution_outcome"]["operation_entered"] is None
            after_payload, before_payload = read_json_file(canonical), json.loads(before)
            # 原scope仍活，main模型失败进度可以追加；unknown不能改原身份/活动轮、产生runner结果或业务终态。
            for field in ("id", "root_id", "parent_id", "owner", "status", "runner_active_attempt_id", "runner_attempts", "runner_abandoned_attempt_ids"):
                assert after_payload[field] == before_payload[field]
            assert after_payload["status"] == "RUNNING" and after_payload["runner_active_attempt_id"]
            assert not Path(after_payload["runner_result_json"]).exists()
            assert p3.read_bytes() == before_p3
        assert bounded_call_error_origin(error) is None
        observed = claim.path.read_bytes()
        terminalize_unhandled_claimed_gateway_request(gate.paths, claim.path, error, agent=agent,
            expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        assert claim.path.read_bytes() == observed and p3.read_bytes() == before_p3
        assert not (gate.paths.terminal / claim.path.name).exists()


def test_ordinary_backend_error_with_unknown_code_still_uses_original_child_failure(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (agent, _, backend, gate, claim, _):
        error = RuntimeError("synthetic ordinary backend error")
        error.error_code = BoundedCallExitUnconfirmedError.error_code
        error.operation_entered = None
        error.call_id = "same-code-is-not-an-original-call"
        def fail(actual, prompt, **kwargs):
            actual.calls += 1
            raise error
        monkeypatch.setattr(type(backend), "generate", fail)
        response = run_original(agent, gate, claim)
        assert response["ok"] is True and response["background_result"]["ok"] is False
        assert backend.calls == 1 and "execution_outcome" not in read_json_file(claim.path)
        run_id = read_json_file(claim.path)["metadata"]["background_source"]["source_id"]
        task = agent.subagents.load(run_id)
        assert task.status == "FAILED" and Path(task.runner_result_json).exists()
