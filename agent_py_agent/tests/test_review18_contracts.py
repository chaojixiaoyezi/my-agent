"""Independent E11d probes; fixtures and subprocesses use only pytest's temporary root."""
from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.memory_store import curator_state as cs
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState
from agent_py_agent.tests import test_memory_curator_v2 as fixtures


def lease(pid):
    now = datetime.now(timezone.utc)
    return {
        "lease_id": "memory-curator-lease-" + "a" * 32,
        "run_id": "memory-curator-run-" + "b" * 32,
        "reason": "admin", "reason_generation": 1,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=20)).isoformat(),
        "host": socket.gethostname(), "pid": pid,
    }


def write_lease(root, record):
    store = cs.MemoryCuratorStateStore(root / "memory/curator/state.json")
    state = MemoryCuratorState(active_lease=record)
    store.path.write_text(json.dumps(state.to_dict(), sort_keys=True, indent=2) + "\n")
    return store


def acquire(store):
    return store.acquire(reason="admin", config_revision="r18", lease_seconds=600)


def read_line(proc):
    assert select.select([proc.stdout], [], [], 15)[0], "child did not respond"
    return proc.stdout.readline().strip()


def crash_writer(root, crash_after):
    output = json.loads((root / "output.json").read_text())
    store = fixtures.ConversationStore(root / "conversations")
    service = fixtures._service(root, fixtures._StaticStructuredBackend(output), store)
    write_target = service.committer._write_target
    writes = 0

    def stop_after_write(path, text):
        nonlocal writes
        write_target(path, text)
        writes += 1
        if writes == crash_after:
            raise SystemExit(73)

    service.committer._write_target = stop_after_write
    service.run(reason="admin")
    raise AssertionError("crash seam was not reached")


def crashed_case(tmp_path, crash_after):
    store, thread, message = fixtures._conversation(tmp_path)
    output = fixtures._valid_output(thread.thread_id, message.message_id, message.content)
    (tmp_path / "output.json").write_text(json.dumps(output))
    child = subprocess.Popen([
        sys.executable, str(Path(__file__).resolve()), "crash", str(tmp_path), str(crash_after),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 73, (child.returncode, stdout, stderr)
    service = fixtures._service(tmp_path, fixtures._StaticStructuredBackend(output), store)
    active = service.state_store.load().active_lease
    assert active["pid"] == child.pid
    assert datetime.fromisoformat(active["expires_at"]) > datetime.now(timezone.utc)
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    manifests = list(service.committer.transactions_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    return service, thread, message, manifests[0]


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_restart_never_executes_over_unrecovered_partial_transaction(tmp_path, crash_after):
    service, _, _, manifest = crashed_case(tmp_path, crash_after)
    result = service.run(reason="admin")
    records = service.run_log.list()
    facts = {"status": result.status, "failure_code": result.failure_code,
             "orphan_manifest": manifest.exists(), "audit": [r.status for r in records]}
    print("FIRST_RESTART", json.dumps(facts, sort_keys=True))
    # Legacy code may wait for TTL; early recovery must clean the old transaction first.
    if result.status == "busy":
        assert manifest.exists() and service.backend.calls == 0
        return
    assert result.status == "succeeded", facts
    assert not manifest.exists(), facts
    assert any(r.status == "recovered_rollback" for r in records), facts


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_restart_still_recovers_after_next_tick_and_expiry(tmp_path, crash_after):
    service, thread, message, _ = crashed_case(tmp_path, crash_after)
    first = service.run(reason="admin")
    second = service.run(reason="admin", now=datetime.now(timezone.utc) + timedelta(hours=2))
    facts = {"first": first.status, "second": second.status, "failure_code": second.failure_code,
             "manifests": len(list(service.committer.transactions_dir.glob("*/manifest.json")))}
    print("NEXT_RESTART", json.dumps(facts, sort_keys=True))
    assert second.status == "succeeded", facts
    assert not list(service.committer.transactions_dir.glob("*/manifest.json")), facts
    state = service.state_store.load()
    assert state.per_thread_cursors[thread.thread_id] == message.message_id
    assert len(service.candidate_service.list()) == 1


@pytest.mark.parametrize("model_fails", [False, True])
def test_dead_lease_full_service_can_record_result_without_transaction(tmp_path, model_fails):
    store, thread, message = fixtures._conversation(tmp_path)
    output = fixtures._valid_output(thread.thread_id, message.message_id, message.content)
    backend = fixtures._FailingBackend() if model_fails else fixtures._StaticStructuredBackend(output)
    service = fixtures._service(tmp_path, backend, store)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    assert dead.wait(timeout=10) == 0
    write_lease(tmp_path, lease(dead.pid))
    assert not list(service.committer.transactions_dir.glob("*/manifest.json"))
    result = service.run(reason="admin")
    if result.status == "busy":
        result = service.run(reason="admin", now=datetime.now(timezone.utc) + timedelta(hours=2))
    records = service.run_log.list()
    facts = {"status": result.status, "failure_code": result.failure_code,
             "audit": [r.status for r in records], "active_lease": service.state_store.load().active_lease}
    print("NO_TRANSACTION", json.dumps(facts, sort_keys=True))
    assert result.failure_code != "CURATOR_RUN_AUDIT_FAILED", facts
    assert result.status == ("failed" if model_fails else "succeeded"), facts
    assert len(records) == 1, facts
    assert records[0].recovery["kind"] in {"stale_lease_reclaimed", "expired_lease"}


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_recovery_barrier_precedes_new_execution_independent_of_audit(tmp_path, monkeypatch, crash_after):
    service, _, _, manifest = crashed_case(tmp_path, crash_after)
    snapshots = []

    def inspect_before_execution(context):
        snapshots.append({"old_manifest_present": manifest.exists(),
                          "recovery_count": sum(r.status == "recovered_rollback" for r in service.run_log.list())})
        # No new batch or audit is executed, isolating recovery order from the audit schema bug.
        return service._result(status="busy", reason="admin")

    monkeypatch.setattr(service, "_execute", inspect_before_execution)
    service.run(reason="admin")
    print("PRE_EXECUTION", json.dumps(snapshots, sort_keys=True))
    assert all(not row["old_manifest_present"] and row["recovery_count"] == 1 for row in snapshots), snapshots


def contender(path):
    store = cs.MemoryCuratorStateStore(path)
    print("ready", flush=True)
    sys.stdin.readline()
    result = acquire(store)
    print(json.dumps({"acquired": result is not None, "pid": os.getpid()}), flush=True)
    # Keep the winner alive until both decisions are observed; otherwise two serial
    # successful handovers would be valid, not a concurrency violation.
    sys.stdin.readline()


def release_contenders(store, children):
    """在 state 锁内等竞争子进程就绪后放行，保证两个子进程同时抢同一租约。"""
    with locked_json_path(store.path):
        assert [read_line(p) for p in children] == ["ready", "ready"]
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()


def test_two_real_processes_reclaim_exactly_once(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    assert dead.wait(timeout=10) == 0
    store = write_lease(tmp_path, lease(dead.pid))
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([
                sys.executable, str(Path(__file__).resolve()), "contend", str(store.path),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        release_contenders(store, children)
        results = [json.loads(read_line(p)) for p in children]
        assert sum(result["acquired"] for result in results) == 1, results
        active = store.load().active_lease
        winner = next(result for result in results if result["acquired"])
        assert active["pid"] == winner["pid"]
        assert active["recovery"]["previous_pid"] == dead.pid
        assert active["recovery"]["kind"] == "stale_lease_reclaimed"
    finally:
        for child in children:
            stdout, stderr = child.communicate("done\n", timeout=20)
            assert child.returncode == 0, (stdout, stderr)


@pytest.mark.parametrize("probe_result", ["alive", "permission", "io"])
def test_uncertain_or_reused_pid_never_reclaimed(tmp_path, monkeypatch, probe_result):
    store = write_lease(tmp_path, lease(7654321))
    before = store.path.read_bytes()

    def probe(pid, signal):
        assert (pid, signal) == (7654321, 0)
        if probe_result == "permission":
            raise PermissionError("fixture")
        if probe_result == "io":
            raise OSError("fixture")

    monkeypatch.setattr(cs.os, "kill", probe)
    assert acquire(store) is None
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("pid", [None, True, -1, 0, "1234"])
def test_invalid_pid_never_probed(tmp_path, monkeypatch, pid):
    store = write_lease(tmp_path, lease(pid))
    monkeypatch.setattr(cs.os, "kill", lambda *args: pytest.fail("invalid pid probed"))
    assert acquire(store) is None


def test_windows_guard_does_not_call_kill(monkeypatch):
    # Replace only this module's os binding; do not simulate the rest of Windows.
    monkeypatch.setattr(cs, "os", SimpleNamespace(
        name="nt", kill=lambda *args: pytest.fail("Windows kill was called")))
    assert cs._pid_definitely_dead(1234) is False


def test_old_state_and_lease_digest_stable(tmp_path):
    record = lease(os.getpid())
    store = write_lease(tmp_path, record)
    before = store.path.read_bytes()
    snapshot = store.load()
    assert store.path.read_bytes() == before
    assert json.dumps(snapshot.to_dict(), sort_keys=True, indent=2).encode() + b"\n" == before

    def digest(obj):
        return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()

    store.request("turn_threshold")
    after = store.load()
    assert digest(after.active_lease) == digest(record)
    assert "process_identity" not in after.active_lease


if __name__ == "__main__":
    if sys.argv[1] == "crash":
        crash_writer(Path(sys.argv[2]), int(sys.argv[3]))
    elif sys.argv[1] == "contend":
        contender(Path(sys.argv[2]))
