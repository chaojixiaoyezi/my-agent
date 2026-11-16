"""W10 CI cross-platform & lifecycle regression tests (`CI-1`, `CI-2`, `CI-7`, `CI-8`, `CI-9`)."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity, nofollow_fs
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
    gateway_short_transaction,
)
from agent_py_agent.agent.conversation import run_claim as run_claim_mod
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.cli import gateway_process
from agent_py_agent.tests.test_gateway_active_input_worker import (
    real_worker_case,
)
from agent_py_agent.tests.test_gateway_active_input_worker import (
    worker as worker,
)


def test_split_existing_anchor_py311_style_and_broken_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_dir = tmp_path / "present"
    target_dir.mkdir()
    missing = target_dir / "child" / "leaf.json"

    # Simulate Python 3.11 Path.exists(self) signature without follow_symlinks keyword.
    def _exists_no_kw(self: Path) -> bool:
        return os.path.exists(self)

    monkeypatch.setattr(Path, "exists", _exists_no_kw)
    anchor, parts = nofollow_fs.split_existing_anchor(missing)
    assert anchor == target_dir
    assert parts == ("child", "leaf.json")

    if hasattr(os, "symlink"):
        broken = target_dir / "dangling-link"
        try:
            broken.symlink_to(target_dir / "nonexistent-target")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable in this environment")
        assert nofollow_fs.split_existing_anchor(broken) == (broken, ())
        assert nofollow_fs.split_existing_anchor(broken / "sub" / "file.txt") == (broken, ("sub", "file.txt"))


def test_attach_cold_start_settled_intent_handles_slotless_and_frozen_contexts() -> None:
    ready = threading.Event()

    # Slotless object() must not raise AttributeError when metadata cannot be attached.
    gateway_process._attach_cold_start_settled_intent(object(), ttl_seconds=90, ready_event=ready)

    @dataclass(frozen=True)
    class _FrozenContext:
        cold_start_settled_ttl_seconds: int = 0
        cold_start_ready_event: threading.Event | None = None

    frozen = _FrozenContext()
    gateway_process._attach_cold_start_settled_intent(frozen, ttl_seconds=45, ready_event=ready)
    assert frozen.cold_start_settled_ttl_seconds == 45
    assert frozen.cold_start_ready_event is ready

    ns = SimpleNamespace()
    gateway_process._attach_cold_start_settled_intent(ns, ttl_seconds=-5, ready_event=None)
    assert ns.cold_start_settled_ttl_seconds == 0
    assert ns.cold_start_ready_event is None


def test_script_runner_terminate_polls_posix_pgrp_and_preserves_kill_eperm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "posix":
        # Real process group teardown: child spawned inside the script's session must be gone right after _terminate.
        marker = tmp_path / "child_pid.txt"
        script = (
            "import subprocess, sys, time; from pathlib import Path; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"Path({str(marker)!r}).write_text(str(p.pid), encoding='utf-8'); "
            "time.sleep(30)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), "child process did not write its pid"
            child_pid = int(marker.read_text(encoding="utf-8").strip())
            script_runner._terminate(proc)
            assert proc.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(proc.pid, 0)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

        # Polling os.killpg(pid, 0) with EPERM stops polling cleanly; SIGKILL EPERM still bubbles to _cleanup_process.
        fake_proc = SimpleNamespace(pid=999999, returncode=None, wait=lambda timeout=None: 0, poll=lambda: 0)
        calls: list[tuple[int, int]] = []

        def fake_killpg_poll_eperm(pgid: int, sig: int) -> None:
            calls.append((pgid, sig))
            if sig == 0:
                raise PermissionError(errno.EPERM, "eperm on poll")

        monkeypatch.setattr(os, "killpg", fake_killpg_poll_eperm)
        script_runner._terminate(fake_proc)
        assert calls == [(999999, signal.SIGKILL), (999999, 0)]

        def fake_killpg_sigkill_eperm(pgid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise PermissionError(errno.EPERM, "eperm on sigkill")

        monkeypatch.setattr(os, "killpg", fake_killpg_sigkill_eperm)
        cleanup = script_runner._cleanup_process(fake_proc)
        assert cleanup["error_code"] == "SCRIPT_CLEANUP_ERROR"
        assert cleanup["errno"] == errno.EPERM


def test_run_claim_uses_module_monotonic_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [250.0]
    monkeypatch.setattr(run_claim_mod, "_monotonic", lambda: clock[0])

    # Verify stdlib time.monotonic is untouched while ConversationRunClaimHeartbeat uses run_claim._monotonic.
    class _FakeClaims:
        def renew(self, spec: dict) -> dict:
            return spec

    fake_store = SimpleNamespace(claims=_FakeClaims())
    hb = run_claim_mod.ConversationRunClaimHeartbeat(
        {
            "store": fake_store,
            "thread_id": "t-1",
            "claim_scope_id": "s-1",
            "claim_id": "c-1",
            "lease_seconds": 15,
            "interval_seconds": 5.0,
        }
    )
    hb.start()
    try:
        assert hb._lease_deadline == pytest.approx(265.0)
    finally:
        hb.stop()
        hb.join(timeout=2.0)


def test_cache_capacity_reset_restores_default_entries() -> None:
    cache_capacity.configure_from_config(SimpleNamespace(gateway_owner_inflight_limit=256))
    try:
        assert cache_capacity.resident_owners(64) == 256
        cache_capacity.reset_for_tests()
        assert cache_capacity.resident_owners(64) == 64
    finally:
        cache_capacity.reset_for_tests()


def test_ci10b_probe_returns_false_on_same_claim_stop_and_rejects_mismatched_claim(worker) -> None:
    with real_worker_case(worker) as (source, params, agent, claim, _paths, _receipt, _p3):
        assert source.probe(agent, params) is True
        row = read_json_file(claim.path)
        write_json_file_atomic(
            claim.path,
            {**row, "cancel_requested": True, "turn_phase": "closing"},
        )
        # Pure read-only probe returns False on same-claim /stop, while drain stays strict.
        assert source.probe(agent, params) is False
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.drain(agent, params)

        # Mismatched conversation_claim or non-open/non-closing turn_phase must still reject.
        write_json_file_atomic(
            claim.path,
            {**row, "conversation_claim": {"request_id": "foreign"}},
        )
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.probe(agent, params)

        write_json_file_atomic(
            claim.path,
            {**row, "cancel_requested": True, "turn_phase": "closed"},
        )
        with pytest.raises(WorkerDispatchError):
            source.probe(agent, params)


def test_ci10b_noncancelled_closing_claim_raises_outcome_uncertain_on_probe_and_short_scope(worker) -> None:
    with real_worker_case(worker) as (source, params, agent, claim, _paths, _receipt, _p3):
        row = read_json_file(claim.path)
        updated = {key: value for key, value in row.items() if key != "cancel_requested"}
        updated["turn_phase"] = "closing"
        write_json_file_atomic(claim.path, updated)
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.probe(agent, params)
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            with gateway_short_transaction("tool_result"):
                pytest.fail("closing noncancelled short_scope must not enter body")


def test_ci2a_execution_threads_snapshot_retries_and_missing_wake_dir(tmp_path, monkeypatch) -> None:
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.execution_threads import (
        _verify_lifecycle_children,
        lifecycle_session_message_origin,
    )

    home = tmp_path / "owner_home"
    child_dir = home / "tasks" / "child-1"
    child_dir.mkdir(parents=True)
    payload = {
        "id": "child-1",
        "owner": "owner-1",
        "parent_id": "root-1",
        "root_id": "root-1",
        "attributes": {
            "conversation_thread_id": "thr-1",
            "conversation_request_id": "root-1",
        },
    }
    write_json_file_atomic(child_dir / "task.json", payload)

    receiver = SimpleNamespace(owner_home=str(home), owner_id="owner-1")
    store = SimpleNamespace(
        tasks=SimpleNamespace(
            link_report=lambda _tid, _cid: (SimpleNamespace(task_path=str(child_dir), status="active"), None)
        ),
        threads=SimpleNamespace(require=lambda _tid: receiver),
        storage=SimpleNamespace(wake_handled_dir=home / "missing_wake_handled"),
    )
    wake = {
        "reason": "subagent_done",
        "source_agent_id": "child-1",
        "parent_agent_id": "root-1",
        "root_task_id": "root-1",
        "metadata": {"conversation_request_id": "root-1"},
    }

    seen_calls = []
    real_read_bytes_beneath = nofollow_fs.read_bytes_beneath

    def tracked_read_bytes_beneath(*args, **kwargs):
        seen_calls.append(kwargs)
        if kwargs.get("snapshot_retries", 0) > 0 and len(seen_calls) == 1:
            # 模拟读取与原子替换并发重排；read_bytes_beneath 内部重试应恢复。
            write_json_file_atomic(child_dir / "task.json", payload)
        return real_read_bytes_beneath(*args, **kwargs)

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", tracked_read_bytes_beneath)
    _verify_lifecycle_children(store, receiver, "thr-1", "root-1", "root-1", wake)
    expected_retries = 2 if nofollow_fs._supports_dir_fd() else 0
    assert seen_calls and seen_calls[0]["snapshot_retries"] == expected_retries

    assert (
        lifecycle_session_message_origin(
            store,
            "thr-1",
            "root-1",
            {"root_task_id": "root-1"},
            reason="subagent_done",
        )
        is None
    )
    assert json.loads((child_dir / "task.json").read_text(encoding="utf-8"))["id"] == "child-1"


def test_ci2a_subagent_run_index_unavailable_recoverable_and_transient_pending_retry(tmp_path) -> None:
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace

    from agent_py_agent.agent.runtime_errors import runtime_error_report
    from agent_py_agent.agent.subagents.services.persistence.run_index import (
        SubagentRunIndexUnavailable,
    )
    from agent_py_agent.agent.subagents.services.persistence.service import (
        SubAgentPersistenceService,
        _list_runs_for_root_report,
    )

    err = SubagentRunIndexUnavailable("subagent run index has pending canonical commits")
    report = runtime_error_report(err, context="subagents.load_root_index")
    assert report["category"] == "recoverable_runtime"
    assert report["recoverable"] is True

    workspace = tmp_path / "subagents"
    workspace.mkdir(parents=True)
    manager = SimpleNamespace(workspace=workspace, creation_guard=nullcontext)
    service = SubAgentPersistenceService(manager, read_only=False)
    try:
        state = {"calls": 0, "ready_checks": 0}
        real_readiness = service.run_index.readiness
        real_request_ids = service.run_index.request_ids

        def fake_readiness():
            state["ready_checks"] += 1
            if state["calls"] >= 1 and state["ready_checks"] == 2:
                return {"complete": True, "pending": True}
            return real_readiness()

        def fake_request_ids(scope_ids, request_key, epoch):
            state["calls"] += 1
            if state["calls"] == 1:
                raise SubagentRunIndexUnavailable("subagent run index has pending canonical commits")
            return real_request_ids(scope_ids, request_key, epoch)

        service.run_index.readiness = fake_readiness
        service.run_index.request_ids = fake_request_ids

        report_ok = _list_runs_for_root_report(service, "root-task-1")
        assert report_ok.load_errors == []
        assert state["calls"] == 2

        persistent_calls = {"count": 0}

        def always_pending_request_ids(scope_ids, request_key, epoch):
            persistent_calls["count"] += 1
            raise SubagentRunIndexUnavailable("subagent run index has pending canonical commits")

        service.run_index.readiness = lambda: {"complete": True, "pending": True}
        service.run_index.request_ids = always_pending_request_ids
        started = time.monotonic()
        report_fail = _list_runs_for_root_report(service, "root-task-1")
        elapsed = time.monotonic() - started
        assert persistent_calls["count"] == 1
        assert elapsed < 1.0
        assert len(report_fail.load_errors) == 1
        assert report_fail.load_errors[0]["category"] == "recoverable_runtime"
        assert report_fail.load_errors[0]["error_type"] == "SubagentRunIndexUnavailable"
    finally:
        if service._run_index_maintenance_thread is not None:
            service._run_index_maintenance_thread.join(timeout=2)


def test_ci10b_foreground_short_scope_preserves_interrupted_only_for_cancelled_same_attempt(worker) -> None:
    with real_worker_case(worker) as (_source, _params, _agent, claim, paths, _receipt, _p3):
        row = read_json_file(claim.path)
        write_json_file_atomic(
            claim.path,
            {**row, "cancel_requested": True, "turn_phase": "closing"},
        )
        with pytest.raises(InterruptedError):
            with gateway_short_transaction("tool_result"):
                pytest.fail("cancelled short_scope must not enter body")

        # Wrong attempt id must fall back to RequestExecutionOutcomeUncertainError.
        write_json_file_atomic(
            claim.path,
            {**row, "cancel_requested": True, "turn_phase": "closing", "execution_attempt_id": "foreign-attempt"},
        )
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            with gateway_short_transaction("tool_result"):
                pytest.fail("mismatched attempt must not enter body")

        # Terminalized claim must fall back to RequestExecutionOutcomeUncertainError.
        write_json_file_atomic(
            claim.path,
            {**row, "cancel_requested": True, "turn_phase": "closing"},
        )
        paths.terminal.mkdir(parents=True, exist_ok=True)
        write_json_file_atomic(paths.terminal / f"{claim.request_id}.json", {"status": "cancelled"})
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            with gateway_short_transaction("tool_result"):
                pytest.fail("terminalized claim must not enter body")
