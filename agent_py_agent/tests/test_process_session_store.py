"""后台记录事务合同；独立进程只操作临时记录，不充当真实模型或 TUI 验收。"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import directory_lock as lock_module
from agent_py_agent.agent.tooling import background_process_host as host
from agent_py_agent.agent.tooling import process_session_commit as commit_module
from agent_py_agent.agent.tooling import process_session_store as store_module
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_commit import ProcessSessionCommitPendingError
from agent_py_agent.agent.tooling.process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    PROCESS_SESSION_SCHEMA,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionRevisionConflict,
    ProcessSessionStore,
)

SCOPE = ProcessExecutionScope("/owners/example", "thread-a", "task-a", "run-a", "attempt-a")


def test_pending_query_copies_only_matches_and_keeps_full_cache(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_store as module

    store = ProcessSessionStore(tmp_path)
    for index in range(200):
        store.write(_running(f"bg-old-{index}", status="exited", finished_at=100000.0, exit_code=0))
    target = {"store_root": "/conversations", "thread_id": "thread-a", "task_id": "task-a", "run_id": "run-a"}
    for index in range(2):
        store.write(_running(f"bg-pending-{index}", completion_target=target))
    old = time.time() - 5
    os.utime(store.root, (old, old))
    method = getattr(store, "list_pending_completion_records_report", None)
    assert callable(method), "需要存储层内部过滤的固定查询入口"
    copied, original = [], module.deepcopy

    def counted(value):
        if isinstance(value, dict) and "session_id" in value:
            copied.append(value["session_id"])
        return original(value)

    monkeypatch.setattr(module, "deepcopy", counted)
    for cheap in (False, True):
        copied.clear()
        report = method(cheap_checks=cheap)
        assert len(report.records) == 2 and not report.load_errors
        assert sorted(copied) == ["bg-pending-0", "bg-pending-1"]
        report.records[0]["completion_target"]["task_id"] = "mutated"
        assert len(store.list_records()[0]) == 202
        assert len(method(task_id="task-a", cheap_checks=cheap).records) == 2
        assert not method(task_id="other", cheap_checks=cheap).records
        assert not method(owner_key="other", cheap_checks=cheap).records


def test_pending_query_preserves_load_errors_on_every_read_mode(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.record_path("bg-broken").write_text("broken JSON")
    method = getattr(store, "list_pending_completion_records_report", None)
    assert callable(method)
    for cheap in (False, True):
        report = method(cheap_checks=cheap)
        assert not report.records and len(report.load_errors) == 1


def test_ttl_prune_keeps_pending_wakes_and_invalid_or_young_finished_times(tmp_path):
    store = ProcessSessionStore(tmp_path)
    target = {"store_root": "/conversations", "thread_id": "thread-a", "task_id": "task-a", "run_id": "run-a"}
    for name, changes in {
        "old": {}, "retained": {"retain_until_consumed": True},
        "young": {"finished_at": 199999.0, "retain_until_consumed": True},
        "small": {"finished_at": 3.0, "retain_until_consumed": True},
        "pending": {"completion_target": target, "completion_notice_id": "notice-pending"},
        "unreported": {"completion_target": target},
        "consumed": {"completion_target": target, "completion_notice_id": "notice-consumed"},
        "stopped": {"completion_target": target, "completion_notice_id": "explicit_stop"},
    }.items():
        store.write(_running(f"bg-{name}", **{"status": "exited", "exit_code": 0, "finished_at": 100000.0, **changes}))
    activation = {"owner_id": "owner-a", "owner_home": SCOPE.owner_home, "plugin_id": "plugin-a", "activation_id": "a" * 64}
    store.write(_running("bg-activation", status="killed", finished_at=100000.0, exit_code=-9,
        activation_scope=activation, access_scope={"owner_id": "owner-a", "owner_home": SCOPE.owner_home, "conversation_id": ""},
        execution_scope={key: value if key == "owner_home" else "" for key, value in asdict(SCOPE).items()}))
    method = store.prune_finished
    assert "max_age_seconds" in inspect.signature(method).parameters
    removed = method(max_age_seconds=86400.0, now=200000.0, is_notice_consumed=lambda nid: nid == "notice-consumed")
    assert set(removed) == {"bg-old", "bg-retained", "bg-consumed", "bg-stopped", "bg-activation"}
    assert {row["session_id"] for row in store.list_records()[0]} == {"bg-young", "bg-small", "bg-pending", "bg-unreported"}


def test_ttl_prune_rechecks_revision_and_removes_sidecar_only_for_deleted_record(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    store.write(_running(status="exited", exit_code=0, finished_at=100000.0))
    transaction_type = store_module.ProcessSessionTransaction
    original = transaction_type.list_records

    def changed(transaction):
        records, errors = original(transaction)
        if records:
            transaction.write(records[0])
        return records, errors

    assert "max_age_seconds" in inspect.signature(store.prune_finished).parameters
    with monkeypatch.context() as patch:
        patch.setattr(transaction_type, "list_records", changed)
        assert store.prune_finished(max_age_seconds=86400.0, now=200000.0) == []
    assert store.load("bg-a").record
    sidecar = tmp_path / "bg-a.json.lock"
    sidecar.touch()
    assert store.prune_finished(max_age_seconds=86400.0, now=200000.0) == ["bg-a"]
    assert not sidecar.exists()


# LLM: PID 是纯存储测试数据，不作为系统进程身份使用；每个测试显式选择启动阶段。
# 函数用途: 构造一条包含真实协议全部必需字段的预留记录，便于验证坏更新被拒绝。
def _reservation(session_id: str = "bg-a", **changes: object) -> dict[str, object]:
    return {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "revision": 0,
        "access_scope": {
            "owner_id": "owner-a",
            "conversation_id": "thread-a",
            "owner_home": SCOPE.owner_home,
        },
        "execution_scope": asdict(SCOPE),
        "activation_scope": None,
        "retain_until_consumed": False,
        "completion_target": {},
        "completion_notice_id": "",
        "command": "test command",
        "cwd": "/workspace",
        "output_file": "/workspace/command.log",
        "host_state_file": "",
        "launcher_pid": 100,
        "launcher_birth_token": "launcher-birth",
        "pid": 0,
        "pid_birth_token": "",
        "child_pid": 0,
        "child_pid_birth_token": "",
        "reserved_at": 1.0,
        "started_at": 0.0,
        "finished_at": None,
        "exit_code": None,
        "status": "starting",
        "child_launch_started": False,
        "stop_requested": False,
        "handoff_confirmed": False,
        **changes,
    }


# LLM: 此 helper 只制造已绑定记录，不启动操作系统进程；测试不能用它证明真实交接已实现。
# 函数用途: 生成可用于终态和交接竞态验证的完整 running 记录。
def _running(session_id: str = "bg-a", **changes: object) -> dict[str, object]:
    return _reservation(
        session_id,
        **{
            "pid": 101,
            "pid_birth_token": "host-birth",
            "child_pid": 102,
            "child_pid_birth_token": "child-birth",
            "child_launch_started": True,
            "started_at": 2.0,
            "status": "running",
            **changes,
        },
    )


def test_explicit_preparation_cleanup_includes_terminal_but_stays_in_exact_scope(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.write(_running("bg-terminal", status="exited", finished_at=3.0, exit_code=0))
    store.write(_reservation("bg-not-started", status="not_started", finished_at=3.0))
    store.write(_running("bg-other", execution_scope={**asdict(SCOPE), "attempt_id": "another"}))
    assert not store.request_stop(SCOPE).records
    receipt = store.request_stop(SCOPE, include_terminal=True)
    assert {row["session_id"] for row in receipt.records} == {"bg-terminal", "bg-not-started"}
    assert all(row["stop_requested"] for row in receipt.records)
    assert not store.load("bg-other").record["stop_requested"]


def test_missing_reads_do_not_create_store_and_filename_must_match_record(tmp_path):
    store = ProcessSessionStore(tmp_path / "absent")
    assert not store.load("bg-a").record
    assert store.list_records() == ([], [])
    assert not store.root.exists()
    store.write(_reservation())
    store.record_path("bg-a").rename(store.record_path("bg-alias"))
    assert store.load("bg-alias").load_error
    with pytest.raises(ValueError, match="identity differ"):
        store.write(_reservation("bg-alias"))
    assert json.loads(store.record_path("bg-alias").read_text())["session_id"] == "bg-a"


@pytest.mark.parametrize("content", ["{}", "{", "[]"])
def test_bad_existing_record_is_not_overwritten(tmp_path, content):
    store = ProcessSessionStore(tmp_path)
    store.record_path("bg-a").write_text(content)
    assert store.load("bg-a").load_error
    with pytest.raises((ValueError, TypeError)):
        store.write(_reservation())
    assert store.record_path("bg-a").read_text() == content


@pytest.mark.parametrize(
    "changes",
    [
        {"launcher_pid": True},
        {"launcher_birth_token": ""},
        {"reserved_at": float("nan")},
        {"revision": -1},
        {"stop_requested": "true"},
        {"status": "running"},
        {"child_pid": 12, "child_pid_birth_token": "child"},
        {"handoff_confirmed": True},
        {"status": "exited", "finished_at": 3},
        {"finished_at": 3},
        {"exit_code": 0},
        {"child_launch_started": True},
        {"completion_target": {"thread_id": "other"}},
    ],
)
def test_invalid_v2_authority_fails_before_any_directory_creation(tmp_path, changes):
    store = ProcessSessionStore(tmp_path / "authority")
    with pytest.raises((ValueError, TypeError)):
        store.write(_reservation(**changes))
    assert not store.root.exists()


def test_legacy_record_remains_explicit_and_cannot_enter_task_stop(tmp_path):
    store = ProcessSessionStore(tmp_path)
    legacy = _running("bg-legacy", schema=LEGACY_PROCESS_SESSION_SCHEMA)
    legacy.pop("execution_scope")
    store.write(legacy)
    store.write(_reservation())
    receipt = store.request_stop(SCOPE)
    assert [record["session_id"] for record in receipt.records] == ["bg-a"]
    loaded = store.load("bg-legacy").record
    assert loaded["schema"] == LEGACY_PROCESS_SESSION_SCHEMA
    assert "execution_scope" not in loaded
    assert not loaded["stop_requested"]
    store.write({**legacy, "status": "killed", "finished_at": 4})
    assert store.write(legacy)["status"] == "killed"


def test_compare_and_swap_rejects_stale_writer_and_keeps_stop_intent(tmp_path):
    store = ProcessSessionStore(tmp_path)
    old = store.write(_running())
    stopped = store.request_stop(SCOPE).records[0]
    with pytest.raises(ProcessSessionRevisionConflict):
        store.write({**old, "status": "exited", "finished_at": 4})
    final = store.write({**stopped, "stop_requested": False, "status": "killed", "finished_at": 5})
    assert final["stop_requested"]
    assert store.write({**final, "status": "running", "finished_at": None})["status"] == "killed"
    assert store.load("bg-a").record["revision"] == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": 103},
        {"child_pid_birth_token": "different"},
        {"launcher_pid": 999},
        {"host_state_file": "/another"},
        {"command": "different"},
        {"output_file": "/another"},
        {
            "execution_scope": asdict(
                ProcessExecutionScope(SCOPE.owner_home, "thread-a", "other-task")
            )
        },
        {"started_at": 3},
        {"status": "starting"},
    ],
)
def test_bound_process_cannot_be_reassigned(tmp_path, changes):
    store = ProcessSessionStore(tmp_path)
    current = store.write(_running())
    with pytest.raises(ValueError):
        store.write({**current, **changes})
    assert store.load("bg-a").record == current


def test_stop_blocks_late_binding_and_handoff_but_not_a_fresh_reservation(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.write(_reservation())
    stopped = store.request_stop(SCOPE).records[0]
    with pytest.raises(ValueError, match="cannot bind"):
        store.write({**stopped, "pid": 101, "pid_birth_token": "host-birth"})
    store.write(_running("bg-running"))
    store.request_stop(SCOPE)
    with pytest.raises(ValueError, match="handed off"):
        store.write({**store.load("bg-running").record, "handoff_confirmed": True})
    fresh = store.write(
        _reservation("bg-resumed", execution_scope={**asdict(SCOPE), "attempt_id": "attempt-b"})
    )
    assert not fresh["stop_requested"]


def test_stop_between_host_binding_and_child_launch_rejects_launch_checkpoint(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.write(_reservation(pid=101, pid_birth_token="host-birth"))
    stopped = store.request_stop(SCOPE).records[0]
    with pytest.raises(ValueError, match="cannot begin child launch"):
        store.write({**stopped, "child_launch_started": True})
    assert store.load("bg-a").record == stopped


def test_scope_selection_requires_every_provided_identity_and_does_not_infer(tmp_path):
    store = ProcessSessionStore(tmp_path)
    variants = {
        "bg-exact": asdict(SCOPE),
        "bg-other-task": {**asdict(SCOPE), "root_task_id": "task-b"},
        "bg-other-attempt": {**asdict(SCOPE), "attempt_id": "attempt-b"},
        "bg-missing": {**asdict(SCOPE), "root_task_id": ""},
    }
    for session_id, scope in variants.items():
        store.write(_reservation(session_id, execution_scope=scope))
    assert not store.request_stop(ProcessExecutionScope()).records
    receipt = store.request_stop(SCOPE)
    assert [record["session_id"] for record in receipt.records] == ["bg-exact"]
    task_scope = ProcessExecutionScope(SCOPE.owner_home, "thread-a", "task-a")
    assert {record["session_id"] for record in store.request_stop(task_scope).records} == {
        "bg-exact",
        "bg-other-attempt",
    }
    assert not store.load("bg-missing").record["stop_requested"]


def test_unknown_and_starting_are_not_pruned_or_declared_finished(tmp_path):
    store = ProcessSessionStore(tmp_path)
    store.write(_reservation())
    store.write(_running("bg-unknown", status="unknown"))
    store.write(_reservation("bg-not-started", status="not_started", finished_at=3))
    store.prune_finished(0)
    records, errors = store.list_records()
    assert not errors
    assert {record["session_id"] for record in records} == {"bg-a", "bg-unknown"}


@pytest.mark.parametrize("status", ["starting", "running", "unknown"])
def test_unfinished_process_cannot_claim_completion_notice(tmp_path, status):
    store = ProcessSessionStore(tmp_path)
    candidate = _running(status=status) if status == "running" else _reservation(status=status)
    current = store.write(candidate)
    with pytest.raises(ValueError, match="cannot have completion notice"):
        store.write({**current, "completion_notice_id": "unearned-notice"})
    assert store.load("bg-a").record == current


def test_precommit_failure_does_not_publish_stop(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    prior = store.write(_reservation())
    original = commit_module.write_json_file_atomic_unlocked

    def fail_redo(path, record):
        if path.name == ".process-sessions.redo.json":
            raise OSError("injected redo publication failure")
        return original(path, record)

    monkeypatch.setattr(commit_module, "write_json_file_atomic_unlocked", fail_redo)
    with pytest.raises(OSError):
        store.request_stop(SCOPE)
    assert store.load("bg-a").record == prior
    assert not (tmp_path / ".process-sessions.redo.json").exists()


def test_committed_install_failure_preserves_receipt_and_invalidates_open_transaction(
    tmp_path, monkeypatch
):
    store = ProcessSessionStore(tmp_path)
    for sid in ("bg-a", "bg-b"):
        store.write(_reservation(sid))
    original = commit_module.write_json_file_atomic_unlocked

    def fail_second(path, record):
        if path.name == "bg-b.json":
            raise OSError("injected installation failure")
        return original(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "write_json_file_atomic_unlocked", fail_second)
        with store.transaction() as transaction:
            with pytest.raises(ProcessSessionCommitPendingError) as raised:
                transaction.request_stop(SCOPE)
            receipt = raised.value.receipt
            assert receipt.committed and receipt.recovery_required and receipt.transaction_id
            assert {record["session_id"] for record in receipt.records} == {"bg-a", "bg-b"}
            with pytest.raises(RuntimeError, match="no longer active"):
                transaction.load("bg-a")
        report = store.load("bg-a")
        assert not report.record and report.load_error["committed"] is True
    fresh_store = ProcessSessionStore(tmp_path)
    assert all(record["stop_requested"] for record in fresh_store.list_records()[0])
    assert not (tmp_path / ".process-sessions.redo.json").exists()
    fresh_store.write(_reservation("bg-new"))
    assert not fresh_store.load("bg-new").record["stop_requested"]


# LLM: 故障只打在临时 Store 的安装函数，保持已发布 redo 和原记录；不访问产品或真实进程资源。
# 函数用途: 留下一份合法但未安装的停止事务，供恢复与损坏合同测试复用。
def _pending_stop(store, monkeypatch):
    original = commit_module.write_json_file_atomic_unlocked

    def fail_install(path, record):
        if path.name.startswith("bg-"):
            raise OSError("injected failure before first installation")
        return original(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "write_json_file_atomic_unlocked", fail_install)
        with pytest.raises(ProcessSessionCommitPendingError) as raised:
            store.request_stop(SCOPE)
        return raised.value.receipt


@pytest.mark.parametrize(
    "damage", ["truncated", "schema", "last_record", "duplicate", "legacy", "revision", "authority"]
)
def test_invalid_redo_blocks_every_entry_point_without_partial_install(
    tmp_path, monkeypatch, damage
):
    store = ProcessSessionStore(tmp_path)
    originals = {sid: store.write(_reservation(sid)) for sid in ("bg-a", "bg-b")}
    _pending_stop(store, monkeypatch)
    redo_path = tmp_path / ".process-sessions.redo.json"
    redo = json.loads(redo_path.read_text())
    match damage:
        case "schema":
            redo["schema"] = "unrecognized"
        case "last_record":
            redo["entries"][-1]["record"]["launcher_pid"] = False
        case "duplicate":
            redo["entries"][-1] = redo["entries"][0]
        case "legacy":
            redo["entries"][-1]["record"] = _running("bg-b", schema=LEGACY_PROCESS_SESSION_SCHEMA)
        case "revision":
            redo["entries"][-1]["record"]["revision"] += 1
        case "authority":
            redo["entries"][-1]["record"]["command"] = "different command"
    redo_path.write_text("{" if damage == "truncated" else json.dumps(redo))
    assert store.load("bg-a").load_error
    records, errors = store.list_records()
    assert not records and errors
    with pytest.raises((ValueError, TypeError, ProcessSessionCommitPendingError)):
        store.write(_reservation("bg-new"))
    with pytest.raises((ValueError, TypeError, ProcessSessionCommitPendingError)):
        store.prune_finished(0)
    for sid, original in originals.items():
        assert json.loads(store.record_path(sid).read_text()) == original
    assert not store.record_path("bg-new").exists()


def test_redo_conflict_prechecks_all_records_before_first_install(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    original = store.write(_reservation())
    other = store.write(_reservation("bg-b"))
    _pending_stop(store, monkeypatch)
    newer = {**other, "revision": other["revision"] + 5, "stop_requested": True}
    store.record_path("bg-b").write_text(json.dumps(newer))
    report = store.load("bg-a")
    assert not report.record and report.load_error["committed"]
    assert json.loads(store.record_path("bg-a").read_text()) == original
    assert json.loads(store.record_path("bg-b").read_text()) == newer


def test_redo_delete_failure_repeats_same_commit_and_blocks_new_mutation(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path)
    store.write(_reservation())
    unlink = Path.unlink

    def fail_redo_delete(path, *args, **kwargs):
        if path.name == ".process-sessions.redo.json":
            raise OSError("injected deletion failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_redo_delete)
        with pytest.raises(ProcessSessionCommitPendingError) as first:
            store.request_stop(SCOPE)
        committed = json.loads(store.record_path("bg-a").read_text())
        with pytest.raises(ProcessSessionCommitPendingError) as second:
            store.write(_reservation("bg-new"))
        assert second.value.receipt.transaction_id == first.value.receipt.transaction_id
        assert not store.record_path("bg-new").exists()
        assert json.loads(store.record_path("bg-a").read_text()) == committed
    assert store.load("bg-a").record == committed
    assert not (tmp_path / ".process-sessions.redo.json").exists()


def test_completion_receipt_and_terminal_state_survive_later_writer(tmp_path):
    store = ProcessSessionStore(tmp_path)
    current = store.write(_running())
    current = store.write({**current, "handoff_confirmed": True})
    current = store.write(
        {
            **current,
            "status": "exited",
            "finished_at": 4,
            "exit_code": 0,
            "completion_notice_id": "notice-a",
        }
    )
    incoming = {
        **current,
        "status": "running",
        "finished_at": None,
        "exit_code": None,
        "completion_notice_id": "",
        "handoff_confirmed": False,
    }
    final = store.write(incoming)
    assert (
        final["status"],
        final["exit_code"],
        final["handoff_confirmed"],
        final["completion_notice_id"],
    ) == ("exited", 0, True, "notice-a")
    assert not store.request_stop(SCOPE).records


def test_crashed_writer_is_recovered_by_another_process_view_without_rescan(tmp_path):
    store = ProcessSessionStore(tmp_path)
    for sid in ("bg-a", "bg-b"):
        store.write(_reservation(sid))
    script = """
import json, os, sys
from pathlib import Path
from agent_py_agent.agent.tooling import process_session_commit as module
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
original = module.write_json_file_atomic_unlocked
def crash_after_first(path, record):
    original(path, record)
    if path.name == "bg-a.json":
        os._exit(23)
module.write_json_file_atomic_unlocked = crash_after_first
ProcessSessionStore(Path(sys.argv[1])).request_stop(ProcessExecutionScope(**json.loads(sys.argv[2])))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), json.dumps(asdict(SCOPE))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 23, child.stderr
    assert json.loads(store.record_path("bg-a").read_text())["stop_requested"]
    assert not json.loads(store.record_path("bg-b").read_text())["stop_requested"]
    reopened = ProcessSessionStore(tmp_path)
    assert reopened.load("bg-b").record["stop_requested"]
    resumed = reopened.write(_reservation("bg-new"))
    assert not resumed["stop_requested"]
    assert {
        record["revision"]
        for record in reopened.list_records()[0]
        if record["session_id"] != "bg-new"
    } == {2}


def test_directory_lock_serializes_processes_and_keeps_other_roots_independent(tmp_path):
    root = tmp_path / "authority"
    store = ProcessSessionStore(root)
    store.write(_reservation())
    script = """
import sys
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
with ProcessSessionStore(sys.argv[1]).transaction() as transaction:
    record = transaction.load("bg-a")
    print("locked", flush=True)
    sys.stdin.readline()
    transaction.write({**record, "stop_requested": True})
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observed, done = [], threading.Event()

    def read_after_lock():
        observed.append(ProcessSessionStore(root).load("bg-a"))
        done.set()

    reader = threading.Thread(target=read_after_lock, daemon=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        reader.start()
        assert not done.wait(0.1)
        assert ProcessSessionStore(tmp_path / "other").write(_reservation())["revision"] == 1
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        assert done.wait(5)
        assert observed[0].record["stop_requested"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        reader.join(timeout=5) if reader.ident else None


def test_missing_os_lock_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_module, "fcntl", None)
    monkeypatch.setattr(lock_module, "msvcrt", None)
    store = ProcessSessionStore(tmp_path / "unsupported")
    with pytest.raises(RuntimeError, match="OS file lock"):
        store.write(_reservation())
    assert not store.root.exists()


# LLM: 真实直接child用stdin屏障阻止提前退出；只在全部不回收观察断言之后才wait回收。
# 函数用途: 核对运行态为False、退出态可重复读True且Popen尚未回收（覆盖POSIX waitid与Darwin sysctl）。
@pytest.mark.skipif(os.name != "posix" or (sys.platform != "darwin" and not all(hasattr(os, key) for key in
                    ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))), reason="requires POSIX waitid WNOWAIT or Darwin sysctl")
def test_opt6_waitid_observes_real_child_without_reaping():
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert host._direct_child_exited_unreaped(child) is False
        assert child.returncode is None
        child.stdin.close()
        deadline = time.monotonic() + 5
        while host._direct_child_exited_unreaped(child) is False and time.monotonic() < deadline:
            time.sleep(0.01)
        assert host._direct_child_exited_unreaped(child) is True
        assert host._direct_child_exited_unreaped(child) is True
        assert child.returncode is None
    finally:
        child.stdin.close()
        child.wait(timeout=5)
    assert child.returncode == 0


# LLM: 只替换本模块的os引用，不改全局os.name；错误和能力缺失不能触发waitpid/poll。
# 函数用途: 核对waitid被中断时继续轮询，普通系统错误时回退原身份检查。
@pytest.mark.parametrize("error, expected", [(InterruptedError(), False), (OSError("unsupported"), None)])
def test_opt6_waitid_error_fallback(monkeypatch, error, expected):
    calls = []

    # LLM: 精确记录waitid参数后注入系统调用错误。
    # 函数用途: 模拟无权观察或临时中断，不产生真实进程副作用。
    def waitid(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(host, "os", SimpleNamespace(name="posix", P_PID=1, WEXITED=4, WNOHANG=1, WNOWAIT=32, waitid=waitid))
    assert host._direct_child_exited_unreaped(SimpleNamespace(pid=123)) is expected
    assert calls == [(1, 123, 4 | 1 | 32)]


# LLM: 平台或WNOWAIT能力不足必须返回None，而不是把未知当作退出。
# 函数用途: 核对不支持waitid的环境不会尝试系统调用。
@pytest.mark.parametrize("missing", ["waitid", "WNOWAIT", "non_posix"])
def test_opt6_waitid_missing_capability_returns_unknown(monkeypatch, missing):
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="linux"))
    api = {"name": "posix", "P_PID": 1, "WEXITED": 4, "WNOHANG": 1, "WNOWAIT": 32, "waitid": pytest.fail}
    if missing == "non_posix":
        api["name"] = "nt"
    else:
        del api[missing]
    monkeypatch.setattr(host, "os", SimpleNamespace(**api))
    assert host._direct_child_exited_unreaped(SimpleNamespace(pid=123)) is None


# LLM: 正常附着宿主已在reservation核过父身份；此处只测每个tick不再调用出生或终止探测。
# 函数用途: 验证直属父launcher快路径接入_stop_reason，交接前和附着交接后都不重复探测。
@pytest.mark.skipif(os.name != "posix", reason="direct-parent shortcut is POSIX-only")
@pytest.mark.parametrize("handoff", [False, True])
def test_opt6_direct_parent_launcher_avoids_identity_probes(monkeypatch, handoff):
    record = _running(launcher_pid=os.getppid(), launcher_birth_token="already-verified", handoff_confirmed=handoff)
    monkeypatch.setattr(host, "capture_process_birth_token", pytest.fail)
    monkeypatch.setattr(host, "_process_instance_terminated", pytest.fail)
    assert host._launcher_instance_alive(record) is True
    for _ in range(20):
        assert host._stop_reason(record, 0, stop_on_launcher_exit=handoff) == ""


# LLM: 亲子关系改变或非POSIX时保持原birth比对；birth不匹配不能再核无关实例的终止状态。
# 函数用途: 核对非直接父launcher的存活、终止、PID复用及非POSIX回退分支。
@pytest.mark.parametrize("case", ["live", "terminated", "reused", "non_posix"])
def test_opt6_launcher_fallback_checks_original_birth(monkeypatch, case):
    calls = []
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt" if case == "non_posix" else "posix", getppid=lambda: 73 if case == "non_posix" else 74))

    # LLM: 记录精确PID，返回匹配或已复用的出生令牌。
    # 函数用途: 模拟非直属启动者的出生身份观察。
    def capture(pid):
        calls.append(("birth", pid))
        return "reused-birth" if case == "reused" else "launcher-birth"

    # LLM: 只在出生令牌匹配后接收同一PID和原令牌。
    # 函数用途: 模拟启动者实例的终止状态。
    def terminated(pid, birth):
        calls.append(("ended", pid, birth))
        return case == "terminated"

    monkeypatch.setattr(host, "capture_process_birth_token", capture)
    monkeypatch.setattr(host, "_process_instance_terminated", terminated)
    assert host._launcher_instance_alive({"launcher_pid": 73, "launcher_birth_token": "launcher-birth"}) is (case in {"live", "non_posix"})
    expected = [("birth", 73)] + ([] if case == "reused" else [("ended", 73, "launcher-birth")])
    assert calls == expected


# LLM: 用原Store写终态，child/终止回执仅为边界替身，不对真实PID发信号；loop只在None时允许旧身份检查。
# 函数用途: 核对monitor接入三态观察，False继续、True终止、None回退，并保留终止出生令牌。
@pytest.mark.parametrize("observations", [(None,), (False, True), (True,)])
def test_opt6_monitor_uses_unreaped_observer_before_fallback(tmp_path, monkeypatch, observations):
    store = ProcessSessionStore(tmp_path)
    store.write(_running(handoff_confirmed=True))
    remaining, fallback, stopped = list(observations), [], []

    # LLM: 返回脚本化的内核观察结果，不触碰Popen的poll或wait。
    # 函数用途: 让monitor分别走三态退出观察分支。
    def observe(child):
        assert child.pid == 102
        return remaining.pop(0)

    # LLM: 只有不支持内核观察时才允许身份回退。
    # 函数用途: 记录原出生身份检查，发现不必要回退立即失败。
    def ended(pid, birth):
        assert observations == (None,)
        fallback.append((pid, birth))
        return True

    # LLM: 终止必须带原child句柄和原birth；替身不发信号，不冒充真实树回收验证。
    # 函数用途: 返回正式回执形状，供真实Store终态落盘。
    def terminate(pid, child, **kwargs):
        stopped.append((pid, child.pid, kwargs))
        return ProcessTerminationReceipt("already_gone", True, 0, 1)

    monkeypatch.setattr(host, "_direct_child_exited_unreaped", observe)
    monkeypatch.setattr(host, "_process_instance_terminated", ended)
    monkeypatch.setattr(host, "terminate_process_tree", terminate)
    monkeypatch.setattr(host, "_HOST_POLL_SECONDS", 0)
    assert host._monitor_child(store, "bg-a", SimpleNamespace(pid=102), "child-birth", 0,
                               host._MonitorPolicy(listen_scope="lan")) == 0
    assert remaining == []
    assert fallback == ([(102, "child-birth")] if observations == (None,) else [])
    assert stopped == [(102, 102, {"expected_birth_token": "child-birth", "grace_seconds": 0.5})]
    assert store.load("bg-a").record["status"] == "exited"


# LLM: 真实Popen、不回收观察、terminate_process_tree和Store；仅拦截提前poll/wait及热路径旧探测，不替换树回收结果。
#   真树入口需要原出生令牌及进程表能力，沿公共能力门；受限环境不冒充已完成该整链验证，强制模式仍须真跑。
# 函数用途: 验证运行到退出的monitor保留组长出生身份直到正式终止入口，且不再重复身份探测。
@pytest.mark.skipif(os.name != "posix" or (sys.platform != "darwin" and not all(hasattr(os, key) for key in
                    ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))), reason="requires POSIX waitid WNOWAIT or Darwin sysctl")
@pytest.mark.sandbox_capability("ps", "background_launcher_identity")
def test_opt6_real_monitor_preserves_child_until_tree_termination(tmp_path, monkeypatch):
    parent_pid = os.getppid()
    parent_birth = host.capture_process_birth_token(parent_pid)
    host._require_reservation(_reservation(launcher_pid=parent_pid, launcher_birth_token=parent_birth))
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    poll, wait, observe, terminate = child.poll, child.wait, host._direct_child_exited_unreaped, host.terminate_process_tree
    observations, terminating = [], []
    try:
        birth = host.capture_process_birth_token(child.pid)
        assert birth and parent_birth
        store = ProcessSessionStore(tmp_path)
        store.write(_running(launcher_pid=parent_pid, launcher_birth_token=parent_birth,
                             child_pid=child.pid, child_pid_birth_token=birth, handoff_confirmed=True))

        # LLM: 第一轮child被stdin屏障保活，观察后释放它；原内核观察及不回收语义不变。
        # 函数用途: 证明真实monitor至少经过运行态和退出态两个分支。
        def observe_child(process):
            result = observe(process)
            observations.append(result)
            if result is False:
                child.stdin.close()
            return result

        # LLM: 只有原终止树入口开始后才允许回收child，原poll保持原行为。
        # 函数用途: 捕获monitor提前poll导致出生身份丢失的回归。
        def guarded_poll(*args, **kwargs):
            assert terminating, "monitor must not poll before tree termination"
            return poll(*args, **kwargs)

        # LLM: wait与poll同样受终止入口顺序约束；真实回收不被mock。
        # 函数用途: 捕获monitor提前wait导致组长被回收的回归。
        def guarded_wait(*args, **kwargs):
            assert terminating, "monitor must not wait before tree termination"
            return wait(*args, **kwargs)

        # LLM: 入终止函数时child仍未回收且已退出；出生令牌沿原调用传入，实际清理照常执行。
        # 函数用途: 核对自然退出进入真实树回收前的身份与顺序。
        def terminate_child(pid, process, **kwargs):
            assert pid == child.pid and process is child and process.returncode is None
            assert kwargs["expected_birth_token"] == birth and observe(child) is True
            terminating.append(pid)
            return terminate(pid, process, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(child, "poll", guarded_poll)
            patch.setattr(child, "wait", guarded_wait)
            patch.setattr(host, "_direct_child_exited_unreaped", observe_child)
            patch.setattr(host, "terminate_process_tree", terminate_child)
            patch.setattr(host, "capture_process_birth_token", pytest.fail)
            patch.setattr(host, "_process_instance_terminated", pytest.fail)
            result = host._monitor_child(store, "bg-a", child, birth, 0,
                                         host._MonitorPolicy(stop_on_launcher_exit=True, listen_scope="lan"))
        assert result == 0 and terminating == [child.pid]
        assert observations[0] is False and observations[-1] is True
        record = store.load("bg-a").record
        assert record["status"] == "exited" and record["exit_code"] == 0 and record["termination"]["confirmed"]
    finally:
        child.stdin.close()
        child.wait(timeout=5)


# LLM: ctypes边界用结构化sysctl替身，不依赖ps或修改原始进程；失败不可推为已退出。
# 函数用途: 核对Darwin状态字节、两阶段失败、空缓冲和短结构的三态结果。
@pytest.mark.parametrize("case, expected", [("live", False), ("zombie", True), ("query_error", None),
    ("read_error", None), ("empty", None), ("short", None), ("exception", None)])
def test_darwin_bghost_sysctl_states(monkeypatch, case, expected):
    calls = []

    # LLM: 精确核对MIB及size指针，模拟内核返回结构，不执行真实系统调用。
    # 函数用途: 为sysctl两阶段读提供状态及错误。
    def sysctl(mib, length, buf, size_ptr, new, new_size):
        calls.append(buf is None)
        assert list(mib) == [1, 14, 1, 123] and length == 4 and new is None and new_size == 0
        if case == "exception":
            raise OSError("sysctl denied")
        size = host.ctypes.cast(size_ptr, host.ctypes.POINTER(host.ctypes.c_size_t)).contents
        size.value = 0 if case == "empty" else 36 if case == "short" else 64
        if buf is not None and size.value >= 37:
            buf[36] = b"\x05" if case == "zombie" else b"\x02"
        return -1 if case == ("query_error" if buf is None else "read_error") else 0

    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(host, "_darwin_libc", lambda: SimpleNamespace(sysctl=sysctl))
    assert host._darwin_child_is_zombie(123) is expected
    assert calls


# LLM: 仅缺少waitid时选择Darwin回退，原waitid平台优先级不变。
# 函数用途: 核对Python3.12形态下的原生观察接线。
def test_darwin_bghost_missing_waitid_uses_sysctl(monkeypatch):
    calls = []

    # LLM: 只观察传入的直接child PID，不模拟回收。
    # 函数用途: 记录Darwin回退调用。
    def zombie(pid):
        calls.append(pid)
        return True

    monkeypatch.setattr(host, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(host, "_darwin_child_is_zombie", zombie)
    assert host._direct_child_exited_unreaped(SimpleNamespace(pid=123)) is True
    assert calls == [123]


# LLM: 真实Store会取目录锁；缓存必须容忍本次load自己的锁目录变化，仍捕捉后续结构变化。
# 函数用途: 核对稳定记录少于0.5秒只完整读一次，TTL、记录、根目录及redo立即失效。
@pytest.mark.parametrize("change", ["ttl", "record", "root", "redo", "corrupt"])
def test_darwin_bghost_record_projection_invalidates(tmp_path, monkeypatch, change):
    store = ProcessSessionStore(tmp_path)
    store.write(_running(handoff_confirmed=True))
    original, calls, clock = store.load, [], [10.0]

    # LLM: 保持真实load及错误报告，只统计完整读取。
    # 函数用途: 记录是否真正绕过了取锁和JSON重读。
    def load(session_id):
        calls.append(session_id)
        return original(session_id)

    monkeypatch.setattr(store, "load", load)
    monkeypatch.setattr(host, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    projection = host._HostRecordProjection()
    first = projection.load(store, "bg-a")
    for index in range(1, 10):
        clock[0] = 10 + index * 0.04
        assert projection.load(store, "bg-a") is first
    assert calls == ["bg-a"]
    if change == "ttl":
        clock[0] = 10.5
    elif change == "record":
        store.request_stop(SCOPE)
    elif change == "root":
        (store.root / "another-entry").touch()
    elif change == "redo":
        (store.root / ".process-sessions.redo.json").write_text("{")
    else:
        store.record_path("bg-a").write_text("{")
    second = projection.load(store, "bg-a")
    assert calls == ["bg-a", "bg-a"] and second is not first
    if change == "record":
        assert second.record["stop_requested"]
    if change in {"redo", "corrupt"}:
        assert second.load_error
        assert projection.load(store, "bg-a").load_error and len(calls) == 3
