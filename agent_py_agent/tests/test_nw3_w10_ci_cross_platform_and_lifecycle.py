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
