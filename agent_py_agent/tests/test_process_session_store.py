"""后台记录事务合同；独立进程只操作临时记录，不充当真实模型或 TUI 验收。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import process_session_commit as commit_module
from agent_py_agent.agent.tooling import process_session_lock as lock_module
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
