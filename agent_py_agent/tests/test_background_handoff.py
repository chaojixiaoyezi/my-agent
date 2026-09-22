"""v2 后台启动与清理开发合同；隔离 OS 进程或明确替身，不替代真实 TUI 验收。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling import process_registry as registry_module
from agent_py_agent.agent.tooling import process_session_cleanup as cleanup_module
from agent_py_agent.agent.tooling.process_registry import (
    ProcessRegistry,
    ProcessSessionAuthorityError,
    ProcessTerminationReceipt,
)
from agent_py_agent.agent.tooling.process_session_cleanup import (
    ProcessSessionCleanupError,
    stop_process_session,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    ProcessSessionTransaction,
)
from agent_py_agent.agent.tooling.shell import _background_launch_failure, _background_start_outcome
from agent_py_agent.tests._managed_process_harness import managed_request


@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_revoked_original_authority_blocks_reservation_spawn_or_handoff(tmp_path, checkpoint):
    calls = []

    def authority():
        calls.append(1)
        if len(calls) == checkpoint:
            raise RuntimeError("original attempt closed")

    request = managed_request(tmp_path, authority_check=authority)
    with pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(request)
    error = caught.value
    assert error.cleanup_confirmed
    assert error.record["child_launch_started"] is (checkpoint == 3)
    assert not error.record["handoff_confirmed"]
    assert error.record["status"] == (
        "killed" if checkpoint == 3 else "not_started" if checkpoint == 2 else "starting"
    )
    result = _background_launch_failure("run_command", error)
    assert result.effect_outcome == ("failed" if checkpoint == 3 else "not_started")


def test_cancel_during_last_authority_read_cleans_pre_handoff_child(tmp_path):
    token = CancellationToken()
    calls = []

    def authority():
        calls.append(1)
        if len(calls) == 3:
            token.cancel("interrupt while authority read returns")

    with bind_cancellation_token(token), pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(managed_request(tmp_path, authority_check=authority))
    error = caught.value
    assert isinstance(error.cause, ToolCancelled)
    assert error.cleanup_confirmed and error.record["stop_requested"]
    assert not error.record["handoff_confirmed"]
    assert registry_module._process_instance_terminated(
        error.record["child_pid"], error.record["child_pid_birth_token"]
    )
    assert _background_launch_failure("run_command", error).error_code == "CANCELLED"


def test_stop_freezes_startup_before_handoff(tmp_path, monkeypatch):
    request = managed_request(tmp_path)
    observed = launch._observe_startup
    frozen = []

    def stop_after_observation(store, session_id, pid, birth, timeout):
        observed(store, session_id, pid, birth, timeout)
        frozen.extend(store.request_stop(request.execution_scope).records)

    monkeypatch.setattr(launch, "_observe_startup", stop_after_observation)
    with pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(request)
    assert [row["session_id"] for row in frozen] == [caught.value.record["session_id"]]
    assert caught.value.cleanup_confirmed
    assert caught.value.record["status"] == "killed"
    assert not caught.value.record["handoff_confirmed"]


def test_launcher_crash_before_handoff_is_recovered_by_host(tmp_path):
    script = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "from agent_py_agent.agent.tooling import background_process_launch as launch",
            "from agent_py_agent.tests._managed_process_harness import managed_request",
            "calls = []",
            "def authority():",
            "    calls.append(1)",
            "    if len(calls) == 3: os._exit(27)",
            f"launch.start_background_process(managed_request(Path({str(tmp_path)!r}), authority_check=authority))",
        ]
    )
    launcher = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], timeout=8
    )
    assert launcher.returncode == 27
    store = ProcessSessionStore(tmp_path / "authority")
    records, errors = store.list_records()
    assert not errors and len(records) == 1
    selected = records[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.load(selected["session_id"]).record
            if current["status"] == "killed":
                break
            time.sleep(0.05)
        assert current["status"] == "killed"
        assert current["reason"] == "launcher_unavailable"
        assert not current["handoff_confirmed"]
        assert current["termination"]["confirmed"]
    finally:
        stop_process_session(store, store.load(selected["session_id"]).record)


def test_explicit_attached_lifetime_stops_after_launcher_crash_even_after_handoff(tmp_path):
    script = "\n".join([
        "import os", "from pathlib import Path",
        "from agent_py_agent.agent.tooling.background_process_launch import start_background_process",
        "from agent_py_agent.tests._managed_process_harness import managed_request",
        f"start_background_process(managed_request(Path({str(tmp_path)!r}), stop_on_launcher_exit=True))",
        "os._exit(28)",
    ])
    launcher = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], timeout=8)
    assert launcher.returncode == 28
    store = ProcessSessionStore(tmp_path / "authority")
    records, errors = store.list_records()
    assert not errors and len(records) == 1
    selected = records[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = store.load(selected["session_id"]).record
            if current["status"] == "killed":
                break
            time.sleep(0.05)
        assert current["handoff_confirmed"]
        assert current["status"] == "killed" and current["reason"] == "launcher_unavailable"
        assert current["termination"]["confirmed"]
    finally:
        stop_process_session(store, store.load(selected["session_id"]).record)


def test_host_enforces_original_deadline_after_handoff_without_parent_polling(tmp_path):
    request = managed_request(tmp_path, deadline_monotonic=time.monotonic() + 3)
    hosted = launch.start_background_process(request)
    store = ProcessSessionStore(request.store_root)
    try:
        hosted.process.wait(timeout=6)
        record = store.load(hosted.record["session_id"]).record
        assert record["status"] == "killed" and record["reason"] == "deadline_exceeded"
        assert record["handoff_confirmed"] and record["termination"]["confirmed"]
    finally:
        stop_process_session(store, store.load(hosted.record["session_id"]).record, host_process=hosted.process)


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf")])
def test_launch_lifetime_rejects_invalid_values_before_store_writes(tmp_path, value):
    with pytest.raises(ValueError):
        managed_request(tmp_path, deadline_monotonic=value)
    assert not (tmp_path / "authority").exists()


def test_expired_deadline_cannot_start_child(tmp_path):
    with pytest.raises(launch.BackgroundLaunchError) as error:
        launch.start_background_process(managed_request(tmp_path, deadline_monotonic=time.monotonic() - 1))
    assert error.value.cleanup_confirmed
    assert not error.value.record["child_launch_started"]


def test_host_loss_does_not_fake_child_exit_and_exact_stop_recovers(tmp_path):
    hosted = launch.start_background_process(managed_request(tmp_path))
    registry = ProcessRegistry()
    record = registry.attach(hosted.record, hosted.process, hosted.store_root)
    try:
        os.kill(hosted.process.pid, signal.SIGKILL)
        hosted.process.wait(timeout=4)
        assert not registry_module._process_instance_terminated(
            record.child_pid, record.persisted_snapshot["child_pid_birth_token"]
        )
        status = registry.status(record.session_id, store_root=hosted.store_root)
        assert status["status"] == "unknown"
        assert registry.wait(record.session_id, 0.02, store_root=hosted.store_root)[
            "wait_timed_out"
        ]
    finally:
        stopped = registry.kill(record.session_id, store_root=hosted.store_root)
        assert stopped["termination"]["confirmed"]
        assert stopped["status"] == "killed"


def test_one_session_stop_does_not_stop_another_session_of_same_task(tmp_path):
    first_request = managed_request(tmp_path / "a")
    second_request = replace(first_request, log_path=tmp_path / "b.log")
    first = launch.start_background_process(first_request)
    second = launch.start_background_process(second_request)
    registry = ProcessRegistry()
    a = registry.attach(first.record, first.process, first.store_root)
    b = registry.attach(second.record, second.process, second.store_root)
    try:
        assert registry.kill(a.session_id)["termination"]["confirmed"]
        assert registry.status(b.session_id)["status"] == "running"
        assert not registry.get(b.session_id).persisted_snapshot["stop_requested"]
    finally:
        registry.kill(a.session_id)
        registry.kill(b.session_id)


def test_healthy_terminal_read_reaps_local_host_without_replacing_child_code(tmp_path):
    hosted = launch.start_background_process(
        managed_request(tmp_path, "import time; time.sleep(1); raise SystemExit(7)")
    )
    registry = ProcessRegistry()
    record = registry.attach(hosted.record, hosted.process, hosted.store_root)
    result = registry.wait(record.session_id, 5)
    assert result["status"] == "exited" and result["exit_code"] == 7
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and hosted.process.returncode is None:
        registry.status(record.session_id)
        time.sleep(0.02)
    assert hosted.process.returncode is not None


def test_hot_cache_reads_current_revision_and_bad_redo_cannot_restore_stale_success(tmp_path):
    request = managed_request(tmp_path)
    store = ProcessSessionStore(request.store_root)
    row = store.write(launch._reservation(request, "bg-shared"))
    registry = ProcessRegistry()
    record = registry.get(row["session_id"], store_root=store.root)
    assert record.status == "starting"
    changed = store.write({**row, "stop_requested": True})
    assert registry.get(row["session_id"], store_root=store.root).to_record() == changed
    (store.root / ".process-sessions.redo.json").write_text("bad redo")
    with pytest.raises(ProcessSessionAuthorityError):
        registry.status(row["session_id"], store_root=store.root)
    rows, errors = registry.list_report(store_root=store.root)
    assert rows == [] and errors


def test_same_session_id_in_two_stores_remains_separate(tmp_path):
    registry = ProcessRegistry()
    for name in ("a", "b"):
        request = managed_request(tmp_path / name)
        store = ProcessSessionStore(request.store_root)
        payload = store.write(launch._reservation(request, "bg-same"))
        assert registry.get("bg-same", store_root=store.root).to_record() == payload
    assert registry.get("bg-same") is None
    assert registry.get(
        "bg-same", store_root=tmp_path / "a" / "authority"
    ).access_scope.owner_home == str(tmp_path / "a")
    assert registry.get(
        "bg-same", store_root=tmp_path / "b" / "authority"
    ).access_scope.owner_home == str(tmp_path / "b")


@pytest.mark.parametrize("birth", ["old-birth", ""])
def test_frozen_birth_is_checked_inside_termination_before_any_signal(monkeypatch, birth):
    monkeypatch.setattr(
        registry_module, "_process_tree_snapshot", lambda pid: ({pid: "new-birth"}, True)
    )
    monkeypatch.setattr(
        registry_module,
        "_signal_process_snapshot",
        lambda *_args: pytest.fail("must not signal reused PID"),
    )
    result = registry_module.terminate_process_tree(8123, None, expected_birth_token=birth)
    assert not result.confirmed
    assert result.unresolved_pids == (8123,)


def _bound_record(tmp_path, status="running"):
    request = managed_request(tmp_path)
    record = launch._reservation(request, "bg-bound")
    record.update(
        pid=8123,
        pid_birth_token="host-birth",
        child_pid=8124,
        child_pid_birth_token="child-birth",
        child_launch_started=True,
        started_at=time.time(),
        status=status,
        finished_at=time.time() if status == "exited" else None,
    )
    store = ProcessSessionStore(request.store_root)
    return store, store.write(record)


def test_unresolved_descendant_cannot_be_hidden_by_terminal_parent_status(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path, "exited")
    receipt = ProcessTerminationReceipt("SIGTERM->SIGKILL", False, 0, 3, (8125,))
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_args: (receipt,))
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_args: True)
    result = stop_process_session(store, record)
    assert not result.confirmed and result.terminations[0].unresolved_pids == (8125,)


def test_cleanup_after_signals_preserves_receipts_when_final_save_fails(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path)
    receipt = ProcessTerminationReceipt("SIGTERM", True, -15, 2)
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_args: (receipt,))
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_args: True)
    original = ProcessSessionTransaction.write

    def fail_final(self, payload):
        if payload["status"] == "killed":
            raise OSError("final write unavailable")
        return original(self, payload)

    monkeypatch.setattr(ProcessSessionTransaction, "write", fail_final)
    with pytest.raises(ProcessSessionCleanupError) as caught:
        stop_process_session(store, record)
    assert caught.value.report["committed"] is True
    assert caught.value.report["termination_receipts"][0]["confirmed"] is True
    assert store.load(record["session_id"]).record["stop_requested"]
    assert store.load(record["session_id"]).record["status"] == "running"


def test_uncertain_cleanup_cannot_use_old_reservation_as_not_started(tmp_path):
    record = launch._reservation(managed_request(tmp_path), "bg-unreadable")
    error = launch.BackgroundLaunchError(OSError("authority unavailable"), record, False)
    outcome = _background_launch_failure("run_command", error)
    assert not outcome.ok and outcome.effect_outcome == "unknown"
    assert json.loads(outcome.output)["status"] == "unknown"


@pytest.mark.parametrize(
    "status,code,ok",
    [("killed", 0, False), ("exited", 0, True), ("exited", None, False), ("unknown", None, False)],
)
def test_initial_result_does_not_promote_killed_or_unknown_to_success(
    tmp_path, monkeypatch, status, code, ok
):
    from agent_py_agent.agent.tooling import shell

    record = SimpleNamespace(session_id="bg-summary", access_scope=None, store_root=str(tmp_path))
    monkeypatch.setattr(
        shell.process_registry, "status", lambda *_args: {"status": status, "exit_code": code}
    )
    result = _background_start_outcome(
        tool_name="run_command", record=record, log_path=tmp_path / "log"
    )
    assert result.ok is ok
    payload = json.loads(result.output)
    assert payload["status"] == status
    if code is not None:
        assert payload["exit_code"] == code


def test_other_pending_transaction_is_not_this_sessions_stop_commit(tmp_path):
    from agent_py_agent.agent.tooling.process_session_commit import (
        ProcessSessionCommitPendingError,
        ProcessSessionCommitReceipt,
    )

    request = managed_request(tmp_path)
    selected = launch._reservation(request, "bg-selected")
    other = {**launch._reservation(request, "bg-other"), "revision": 1, "stop_requested": True}
    error = ProcessSessionCleanupError(
        ProcessSessionCommitPendingError(
            ProcessSessionCommitReceipt("other-transaction", (other,), True)
        ),
        selected,
        (),
        False,
    )
    assert error.report["committed"] is False
    assert error.report["recovery_required"] is True
    assert error.report["recovery_transaction_id"] == "other-transaction"
    assert "transaction_id" not in error.report
    assert error.report["session_ids"] == ["bg-selected"]
    assert error.record == selected
    assert "bg-other" not in json.dumps(error.report)


def test_pending_stop_commit_is_reported_without_signalling_or_losing_receipt(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.tooling import process_session_commit as commit

    store, record = _bound_record(tmp_path)
    original = commit.write_json_file_atomic_unlocked

    def fail_record(path, payload):
        if path.name == f"{record['session_id']}.json":
            raise OSError("record install interrupted")
        return original(path, payload)

    monkeypatch.setattr(commit, "write_json_file_atomic_unlocked", fail_record)
    monkeypatch.setattr(
        cleanup_module,
        "_terminate_frozen_instances",
        lambda *_args: pytest.fail("must recover authority before signals"),
    )
    with pytest.raises(ProcessSessionCleanupError) as caught:
        stop_process_session(store, record)
    assert caught.value.report["committed"] is True
    assert caught.value.report["recovery_required"] is True
    assert caught.value.report["transaction_id"]
    assert caught.value.report["termination_receipts"] == []
    assert caught.value.record["stop_requested"]
    monkeypatch.setattr(commit, "write_json_file_atomic_unlocked", original)
    assert store.load(record["session_id"]).record["stop_requested"]


def test_refresh_failure_does_not_escape_tool_or_use_running_cache(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool

    request = managed_request(tmp_path)
    store = ProcessSessionStore(process_session_store_root(tmp_path, tmp_path))
    record = launch._reservation(request, "bg-unobserved")
    record.update(
        pid=99999998,
        pid_birth_token="missing-host",
        child_launch_started=True,
        child_pid=99999999,
        child_pid_birth_token="missing-child",
        started_at=time.time(),
        status="running",
    )
    store.write(record)
    original = ProcessSessionTransaction.write

    def fail_unknown(self, payload):
        if payload["status"] == "unknown":
            raise OSError("unknown observation save unavailable")
        return original(self, payload)

    monkeypatch.setattr(ProcessSessionTransaction, "write", fail_unknown)
    result = ProcessSessionTool(tmp_path, tmp_path).execute(
        {
            "action": "status",
            "session_id": record["session_id"],
            "__run_scope": {
                "owner_id": "owner-test",
                "session_id": "thread-test",
                "owner_home": str(tmp_path),
            },
        }
    )
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown"
    assert result.result_envelope["load_error"]["error_type"] == "OSError"


def test_no_host_created_can_be_stopped_again_only_with_cleanup_proof(tmp_path):
    calls = []

    def authority():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("revoked before host")

    request = managed_request(tmp_path, authority_check=authority)
    with pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(request)
    record = caught.value.record
    assert record["pid"] == 0 and record["status"] == "not_started"
    assert stop_process_session(ProcessSessionStore(request.store_root), record).confirmed


def test_other_owners_corrupt_store_does_not_break_new_handoff(tmp_path):
    registry = ProcessRegistry()
    old_store, old_record = _bound_record(tmp_path / "old", "exited")
    assert registry.get(old_record["session_id"], store_root=old_store.root).is_terminal()
    (old_store.root / ".process-sessions.redo.json").write_text("broken other store")
    hosted = launch.start_background_process(managed_request(tmp_path / "new"))
    try:
        record = registry.attach(hosted.record, hosted.process, hosted.store_root)
        assert record.status == "running"
    finally:
        assert registry.kill(hosted.record["session_id"], store_root=hosted.store_root)["termination"]["confirmed"]
