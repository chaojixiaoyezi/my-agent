"""仓外非作者探针：原Thread确实出生，ACK异常不能在仍活时把child收为普通FAILED。"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)


@pytest.mark.parametrize("kind", ["heartbeat", "timeout_worker"])
def test_born_original_child_thread_ack_failure_keeps_transport_open(tmp_path, monkeypatch, kind):
    born, release, completed = threading.Event(), threading.Event(), threading.Event()
    original_start, original_run = threading.Thread.start, threading.Thread.run
    kept = []
    fault = RuntimeError("synthetic original child Thread ACK failure")

    def selected(thread):
        target = getattr(thread, "_target", None)
        return (
            thread.name.startswith("runner-session-")
            if kind == "heartbeat"
            else getattr(target, "__module__", "").endswith("agent_core.runner.worker")
            and getattr(target, "__name__", "") == "_target"
        )

    def held_run(thread):
        if selected(thread):
            born.set()
            assert release.wait(8)
        return original_run(thread)

    def lost_ack(thread):
        chosen = selected(thread)
        if chosen:
            stop = thread._args[2] if kind == "heartbeat" else None
            kept.append((thread, stop))
        original_start(thread)
        if chosen:
            assert born.wait(3)
            raise fault

    monkeypatch.setattr(threading.Thread, "run", held_run)
    monkeypatch.setattr(threading.Thread, "start", lost_ack)
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (
        agent,
        _,
        backend,
        gate,
        claim,
        _,
    ):
        agent.config.runner_timeout_by_role = {"*": "1.0"}
        run_id = read_json_file(claim.path)["metadata"]["background_source"]["source_id"]
        p3 = reservation_file(gate.paths, claim.request_id)
        before_p3 = p3.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            future.add_done_callback(lambda _: completed.set())
            try:
                assert born.wait(5), (
                    future.result(1) if future.done() else "original thread not born"
                )
                prematurely_closed = completed.wait(0.3)
                error, response = None, None
                if future.done():
                    try:
                        response = future.result()
                    except BaseException as failure:
                        error = failure
                task = agent.subagents.load(run_id)
                canonical = Path(task.agent_run_workspace_dir) / "canonical_state.json"
                payload = read_json_file(claim.path)
                facts = {
                    "kind": kind,
                    "original_thread_alive": kept[0][0].is_alive(),
                    "request_returned_before_original_thread_exit": prematurely_closed,
                    "response": response,
                    "error_type": type(error).__name__ if error is not None else None,
                    "backend_calls": backend.calls,
                    "canonical_status": task.status,
                    "canonical_sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
                    "runner_result_exists": Path(task.runner_result_json).exists(),
                    "request_status": payload.get("status"),
                    "execution_outcome": payload.get("execution_outcome"),
                    "p3_unchanged": p3.read_bytes() == before_p3,
                    "terminal_exists": (gate.paths.terminal / claim.path.name).exists(),
                }
                (tmp_path / ("ACK_" + kind + "_FACTS.json")).write_text(
                    json.dumps(facts, ensure_ascii=False, indent=2) + "\n"
                )
                assert not prematurely_closed, facts
            finally:
                for _thread, stop in kept:
                    if stop is not None:
                        stop.set()
                release.set()
                backend.release.set()
                for thread, _stop in kept:
                    thread.join(4)
                    assert not thread.is_alive()
                try:
                    future.result(5)
                except BaseException:
                    pass


@pytest.mark.parametrize("kind", ["heartbeat", "timeout_worker"])
def test_original_ack_and_join_unknown_keeps_same_error_claim_and_no_runner_result(
    tmp_path, monkeypatch, kind
):
    import contextvars
    import copy
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.runner import session_pool
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_errors import (
        runner_request_execution_outcome_uncertain,
    )

    born, release = threading.Event(), threading.Event()
    original_start, original_run, original_join = (
        threading.Thread.start,
        threading.Thread.run,
        threading.Thread.join,
    )
    kept = []
    fault = RuntimeError("synthetic original ACK exception")

    def selected(thread):
        target = getattr(thread, "_target", None)
        return (
            thread.name.startswith("runner-session-")
            if kind == "heartbeat"
            else getattr(target, "__module__", "").endswith("agent_core.runner.worker")
            and getattr(target, "__name__", "") == "_target"
        )

    def held_run(thread):
        if selected(thread):
            born.set()
            assert release.wait(8)
        return original_run(thread)

    def lost_ack(thread):
        chosen = selected(thread)
        if chosen:
            kept.append((thread, thread._args[2] if kind == "heartbeat" else None))
        original_start(thread)
        if chosen:
            assert born.wait(3)
            raise fault

    def unknown_join(thread, *args, **kwargs):
        if kept and thread is kept[0][0] and not release.is_set():
            raise RuntimeError("synthetic original join unconfirmed")
        return original_join(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "run", held_run)
    monkeypatch.setattr(threading.Thread, "start", lost_ack)
    monkeypatch.setattr(threading.Thread, "join", unknown_join)
    with curator_case(tmp_path, monkeypatch, source_kind="orphan") as (
        agent,
        _,
        backend,
        gate,
        claim,
        _,
    ):
        agent.config.runner_timeout_by_role = {"*": "1.0"}
        payload = read_json_file(claim.path)
        run_id = payload["metadata"]["background_source"]["source_id"]
        p3 = reservation_file(gate.paths, claim.request_id)
        before_p3 = p3.read_bytes()
        from agent_py_agent.agent.gateway_parts import (
            multiprocess_subagent_execution as child_execution,
        )

        original_observe = child_execution._observe_original_child_unknown
        checked_live = []

        def observe_actual(execution, error):
            if error is fault:
                current = read_json_file(claim.path)
                expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
                assert runner_request_execution_outcome_uncertain(error, current, expected)
                assert not runner_request_execution_outcome_uncertain(
                    error, current, ("other", expected[1], expected[2])
                )
                assert not runner_request_execution_outcome_uncertain(
                    error, {**current, "lease_owner": "other"}, expected
                )
                assert not runner_request_execution_outcome_uncertain(
                    error, current, (expected[0], "other", expected[2])
                )
                assert not runner_request_execution_outcome_uncertain(
                    error, current, (expected[0], expected[1], expected[2] + 1)
                )
                checked_live.append(True)
            return original_observe(execution, error)

        monkeypatch.setattr(child_execution, "_observe_original_child_unknown", observe_actual)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_original, agent, gate, claim)
                assert born.wait(5)
                with pytest.raises(RuntimeError) as caught:
                    future.result(5)
            assert caught.value is fault and kept[0][0].is_alive()
            assert checked_live == [True]
            task = agent.subagents.load(run_id)
            assert task.status == "RUNNING" and task.runner_active_attempt_id
            assert not Path(task.runner_result_json).exists() and backend.calls == 0
            actual = read_json_file(claim.path)
            unknown = actual["execution_outcome"]
            phase = (
                "child_runner_heartbeat_stop" if kind == "heartbeat" else "child_runner_worker_stop"
            )
            assert unknown["status"] == "unknown" and unknown["phase"] == phase
            assert unknown["error_code"] == "TASK_RESOURCE_STOP_UNCONFIRMED"
            assert unknown["operation_entered"] is None and "call_id" not in unknown
            assert (
                unknown["runner_run_id"] == run_id
                and unknown["runner_attempt_id"] == task.runner_active_attempt_id
            )
            assert actual["status"] == "processing" and p3.read_bytes() == before_p3
            assert not (gate.paths.terminal / claim.path.name).exists()
            origin = session_pool.runner_thread_error_origin(fault)
            assert origin is not None and origin[1:] == (
                phase,
                run_id,
                task.runner_active_attempt_id,
            )
            assert session_pool.runner_thread_error_origin(copy.copy(fault)) is None
            spoof = RuntimeError("ordinary same code")
            spoof.error_code = "TASK_RESOURCE_STOP_UNCONFIRMED"
            assert session_pool.runner_thread_error_origin(spoof) is None
            assert not session_pool.runner_thread_exit_unconfirmed(spoof)
            with coordinator_execution_scope(gate, claim, agent):
                # 原child scope已经关闭，父对象不能替代它补一条事件。
                assert not runner_request_execution_outcome_uncertain(
                    fault, actual, (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
                )
            thread = kept[0][0]
            captured = thread._runner_thread_origin
            thread._runner_thread_origin = copy.copy(captured)
            assert session_pool.runner_thread_error_origin(fault) is None
            thread._runner_thread_origin = captured
            old = thread._runner_thread_context
            thread._runner_thread_context = contextvars.copy_context()
            assert session_pool.runner_thread_error_origin(fault) is None
            thread._runner_thread_context = old
            old_started = thread._started
            thread._started = threading.Event()
            assert session_pool.runner_thread_error_origin(fault) is None
            thread._started = old_started
            old_metadata = BaseException.__dict__["__dict__"].__get__(fault)[
                "_runner_thread_origin"
            ]
            BaseException.__dict__["__dict__"].__get__(fault)["_runner_thread_origin"] = replace(
                captured
            )
            assert session_pool.runner_thread_error_origin(fault) is None
            BaseException.__dict__["__dict__"].__get__(fault)["_runner_thread_origin"] = (
                old_metadata
            )
            assert session_pool.runner_thread_error_origin(fault) is not None
            with monkeypatch.context() as local:
                local.setattr(session_pool.os, "getpid", lambda: -1)
                assert session_pool.runner_thread_error_origin(fault) is None
                assert session_pool.runner_thread_exit_unconfirmed(fault)
            # 同一原异常绝不能被另一尚未出生Thread重绑，私有producer只用于此负例，不执行它。
            another = threading.Thread(target=lambda: None)
            newer = session_pool._capture_runner_thread(
                another, "child_runner_worker_stop", "other-run", "other-attempt"
            )
            with pytest.raises(RuntimeError) as unchanged:
                session_pool._finish_original_runner_thread(newer, lambda: None, fault)
            assert (
                unchanged.value is fault
                and session_pool.runner_thread_error_origin(fault) == origin
            )
            stable = claim.path.read_bytes()
        finally:
            for _, stop in kept:
                if stop is not None:
                    stop.set()
            release.set()
            backend.release.set()
            for thread, _ in kept:
                original_join(thread, 4)
                assert not thread.is_alive()
        assert session_pool.runner_thread_error_origin(fault) is not None
        assert claim.path.read_bytes() == stable and p3.read_bytes() == before_p3
        assert (
            not Path(agent.subagents.load(run_id).runner_result_json).exists()
            and backend.calls == 0
        )
