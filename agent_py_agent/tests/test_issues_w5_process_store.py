"""W5 真实临时存储回归：只验证记录与文件生命周期，不代表真实服务或外部可达验收。"""
from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import stat
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling import process_registry as registry
from agent_py_agent.agent.tooling import process_session_retention as retention
from agent_py_agent.agent.tooling import process_session_store as store_module
from agent_py_agent.agent.tooling.process_cleanup_evidence import process_cleanup_reference
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)

SCOPE = ProcessExecutionScope("/owners/example", "thread-a", "task-a", "run-a", "attempt-a")
NOW = 1800000000.0
DAY = 86400


# 从原 Store 测试复制完整字段，不跨测试模块导入；身份仅用于存储，不作为 OS 退出证明。
def _reservation(session_id="bg-a", **changes):
    return {"schema": PROCESS_SESSION_SCHEMA, "session_id": session_id, "revision": 0,
            "access_scope": {"owner_id": "owner-a", "conversation_id": "thread-a", "owner_home": SCOPE.owner_home},
            "execution_scope": asdict(SCOPE), "activation_scope": None, "retain_until_consumed": False,
            "completion_target": {}, "completion_notice_id": "", "command": "test command", "cwd": "/workspace",
            "output_file": "/workspace/command.log", "host_state_file": "", "launcher_pid": 100,
            "launcher_birth_token": "launcher-birth", "pid": 0, "pid_birth_token": "", "child_pid": 0,
            "child_pid_birth_token": "", "reserved_at": 1.0, "started_at": 0.0, "finished_at": None,
            "exit_code": None, "status": "starting", "child_launch_started": False, "stop_requested": False,
            "handoff_confirmed": False, **changes}


def _running(session_id="bg-a", **changes):
    return _reservation(session_id, **{"pid": 101, "pid_birth_token": "host-birth", "child_pid": 102,
                                     "child_pid_birth_token": "child-birth", "child_launch_started": True,
                                     "started_at": 2.0, "status": "running", **changes})


def _activation(session_id="bg-a", **changes):
    return _running(session_id, **{
        "status": "exited", "finished_at": NOW - 2 * DAY, "exit_code": 0,
        "activation_scope": {"owner_id": "owner-a", "owner_home": SCOPE.owner_home,
                             "plugin_id": "plugin-a", "activation_id": "a" * 64},
        "access_scope": {"owner_id": "owner-a", "owner_home": SCOPE.owner_home, "conversation_id": ""},
        "execution_scope": {key: value if key == "owner_home" else "" for key, value in asdict(SCOPE).items()},
        **changes})


@pytest.fixture
def domain(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    home = SimpleNamespace(owner_home_dir=owner, owner_retention_json=owner / "retention.json")
    store = ProcessSessionStore(store_module.process_session_store_root(owner, owner))
    return home, store


def _request(home, *, now=NOW, cursor=None, budget=None, commit=None, legal_hold=False):
    return StorageLaneRequest("process_sessions", home, now, cursor or {},
                              budget or StorageLaneBudget(None, None, None), legal_hold, commit is not None, None, commit)


def _finish(home, **kwargs):
    cursor = kwargs.pop("cursor", {})
    results = []
    for _ in range(30):
        result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home, cursor=cursor, **kwargs)))
        results.append(result)
        assert not result["errors"], result
        if result["complete"]:
            return results
        cursor = result["cursor"]
    pytest.fail(f"maintenance failed to finish: {results[-1]}")


def _log(home, name="job-1.log", *, root=None):
    directory = (root or home.owner_home_dir) / ".background_jobs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"background output\n")
    return path


def _retire(store, path, *, session="bg-log", finished=NOW - 8 * DAY):
    store.write(_running(session, status="exited", exit_code=0, finished_at=finished, output_file=str(path)))
    assert store.prune_finished(max_age_seconds=DAY, now=NOW) == [session]
    return store.root / ".log_retirement.jsonl"


def test_default_scan_budget_scales_but_explicit_budget_is_strict(tmp_path, monkeypatch):
    store = ProcessSessionStore(tmp_path / "sessions")
    with store.transaction() as transaction:
        for index in range(3000):
            transaction.write(_running(f"bg-{index}"))
        # 6000 余次扫描/读取检查：0.2ms 每次落在固定 1s 与冻结的 1.3s 比例额度之间。
        tick = 0.0
        def monotonic():
            nonlocal tick
            tick += 0.0002
            return tick
        with monkeypatch.context() as patch:
            # 默认额度按扫描线程CPU时间计量，显式额度按墙钟；两者共用同一假时钟。
            patch.setattr(store_module.time, "monotonic", monotonic)
            patch.setattr(store_module.time, "thread_time", monotonic)
            records, errors = transaction.list_records()
            assert len(records) == 3000 and errors == []
            tick = 0.0
            with pytest.raises(RuntimeError, match="process_session_scan_budget_exceeded"):
                transaction.list_records(max_ms=1000)
        # 0.5ms/调用在枚举第约2500项已超过冻结公式，不能声称这个输入会成功。
        with monkeypatch.context() as patch:
            tick = 0.0
            def slower():
                nonlocal tick
                tick += 0.0005
                return tick
            patch.setattr(store_module.time, "thread_time", slower)
            with pytest.raises(RuntimeError, match="process_session_scan_budget_exceeded"):
                transaction.list_records()
    assert "with os.scandir(self.root) as entries:" in inspect.getsource(store_module.ProcessSessionTransaction.list_records)


def test_default_scan_budget_ignores_same_process_wall_clock_stalls(tmp_path, monkeypatch):
    # 现场: 18+ TUI 同进程线程争用 GIL，墙钟远超实际扫描 CPU；默认额度不能因此拒绝整份清单（停止/裁剪/完成协调全失败）。
    store = ProcessSessionStore(tmp_path / "sessions")
    with store.transaction() as transaction:
        for index in range(50):
            transaction.write(_running(f"bg-{index}"))
        wall, cpu = [0.0], [0.0]
        def stalled_wall():
            wall[0] += 5.0
            return wall[0]
        def scan_cpu():
            cpu[0] += 0.00001
            return cpu[0]
        with monkeypatch.context() as patch:
            patch.setattr(store_module.time, "monotonic", stalled_wall)
            patch.setattr(store_module.time, "thread_time", scan_cpu)
            records, errors = transaction.list_records()
            assert len(records) == 50 and errors == []
            with pytest.raises(RuntimeError, match="process_session_scan_budget_exceeded"):
                transaction.list_records(max_ms=1000)


def test_attach_keeps_committed_handoff_when_history_prune_fails(tmp_path, monkeypatch, caplog):
    # 现场: 交接已提交、句柄已挂上后历史裁剪超额抛 RuntimeError；旧行为外抛，shell 把已启动命令报成结果未知并停住回合。
    # 假句柄只提供 poll，从未派生进程或向假 PID 发信号。
    store = ProcessSessionStore(tmp_path / "sessions")
    payload = store.write(_running("bg-attach", pid=987654325, pid_birth_token="attach-birth",
                                   child_pid=987654326, child_pid_birth_token="attach-child-birth"))
    calls = []

    def exhausted(self, *args, **kwargs):
        calls.append(self.root)
        raise RuntimeError("process_session_scan_budget_exceeded")

    monkeypatch.setattr(store_module.ProcessSessionStore, "prune_finished", exhausted)
    handle = SimpleNamespace(pid=987654325, poll=lambda: None)
    process_registry = registry.ProcessRegistry()
    with caplog.at_level(logging.WARNING, logger=registry.__name__):
        record = process_registry.attach(payload, handle, store.root)
    assert calls and record.session_id == "bg-attach" and record.process is handle
    assert "PROCESS_ATTACH_PRUNE_DEFERRED" in caplog.text
    assert process_registry.get("bg-attach", store_root=store.root) is record


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_explicit_invalid_scan_time_is_rejected(tmp_path, value):
    store = ProcessSessionStore(tmp_path)
    with store.transaction() as transaction, pytest.raises(ValueError):
        transaction.list_records(max_ms=value)


def test_activation_ttl_is_exit_confirmed_and_never_count_based(tmp_path):
    store = ProcessSessionStore(tmp_path)
    shapes = {"exited": {}, "host-killed": {"status": "killed", "reason": "deadline_exceeded", "exit_code": -9},
              "stopping": {"stop_requested": True}, "killed": {"status": "killed", "exit_code": -9},
              "retained": {"retain_until_consumed": True}, "small": {"finished_at": 3.0},
              "pending": {"completion_notification_status": "pending"}}
    for name, changes in shapes.items():
        store.write(_activation(f"bg-{name}", **changes))
    assert store.prune_finished(0, max_age_seconds=None) == []
    assert set(store.prune_finished(max_age_seconds=DAY, now=NOW)) == {"bg-exited", "bg-host-killed"}
    assert {row["session_id"] for row in store.list_records()[0]} == {f"bg-{name}" for name in shapes if name not in {"exited", "host-killed"}}


def test_launch_spec_does_not_create_sidecar_on_failed_spawn(domain, monkeypatch):
    home, store = domain
    specs = []
    def fail(*args, **kwargs):
        paths = list((store.root / ".launches").glob("*.json"))
        assert len(paths) == 1
        assert not list((store.root / ".launches").glob("*.lock"))
        specs.append(json.loads(paths[0].read_text()))
        assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(paths[0].parent.stat().st_mode) == 0o700
        raise OSError("before spawning")
    monkeypatch.setattr(launch, "capture_process_birth_token", lambda _: "fixture-launcher-birth")
    monkeypatch.setattr(launch.subprocess, "Popen", fail)
    request = launch.BackgroundLaunchRequest([sys.executable, "-c", "pass"], "test", home.owner_home_dir,
        _log(home), {}, 1000, store.root, ProcessAccessScope("owner-a", "thread-a", str(home.owner_home_dir)),
        ProcessExecutionScope(str(home.owner_home_dir), "thread-a", "task-a"))
    with pytest.raises(launch.BackgroundLaunchError):
        launch.start_background_process(request)
    assert specs[0]["schema"] == launch.LAUNCH_SPEC_SCHEMA
    assert not list((store.root / ".launches").glob("*.lock"))
    assert not list((store.root / ".launches").glob("*.json"))


def test_retirement_is_durable_before_metadata_unlink(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    store.write(_running(status="exited", exit_code=0, finished_at=NOW - 8 * DAY, output_file=str(path)))
    events = []
    original_sync, original_unlink = store_module.os.fsync, store_module.unlink_file_beneath
    def synced(fd):
        events.append("sync")
        return original_sync(fd)
    def removed(root, parts):
        if parts == ("bg-a.json",):
            assert "sync" in events
            ledger = root / ".log_retirement.jsonl"
            row = json.loads(ledger.read_text())
            assert row["dev"] == path.lstat().st_dev and row["ino"] == path.lstat().st_ino
            assert row["retire_after"] == NOW - DAY
            assert store.record_path("bg-a").exists()
            events.append("unlink")
        return original_unlink(root, parts)
    monkeypatch.setattr(store_module.os, "fsync", synced)
    monkeypatch.setattr(store_module, "unlink_file_beneath", removed)
    assert store.prune_finished(max_age_seconds=DAY, now=NOW) == ["bg-a"]
    assert events.index("sync") < events.index("unlink") and path.exists()
    assert store_module._LOG_RETIREMENT_DEFAULT_DAYS == retention.BACKGROUND_JOB_LOG_RETENTION_DAYS


def test_append_failure_keeps_unique_locator(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    store.write(_running(status="exited", exit_code=0, finished_at=NOW - 8 * DAY, output_file=str(path)))
    def fail(*args, **kwargs):
        raise OSError("append failed")
    monkeypatch.setattr(store_module, "append_private_jsonl_records_unlocked", fail)
    assert store.prune_finished(max_age_seconds=DAY, now=NOW) == []
    assert store.load("bg-a").record and path.exists()
    assert not (store.root / ".log_retirement.jsonl").exists()


@pytest.mark.parametrize("age,deleted", [(8, True), (6, False)])
@pytest.mark.parametrize("external", [False, True])
def test_lane_log_ttl_owner_and_external_workspace(domain, tmp_path, age, deleted, external):
    home, store = domain
    path = _log(home, root=tmp_path / "external" if external else None)
    _retire(store, path, finished=NOW - age * DAY)
    results = _finish(home)
    assert path.exists() is not deleted
    assert sum(row["deleted_items"] for row in results) == int(deleted)
    assert sum(row["freed_bytes"] for row in results) == (len(b"background output\n") if deleted else 0)
    assert not store.load("bg-log").record


def test_owner_retention_override_retires_after_one_day(domain):
    home, store = domain
    home.owner_retention_json.write_text(json.dumps({"background_job_log_days": 1}))
    path = _log(home)
    _retire(store, path, finished=NOW - 2 * DAY)
    _finish(home)
    assert not path.exists()


@pytest.mark.parametrize("change", ["replaced", "symlink", "outside", "hardlink", "parent_symlink", "missing"])
def test_changed_or_unsafe_logs_are_kept_and_rows_complete(domain, tmp_path, monkeypatch, request, change):
    home, store = domain
    path = _log(home)
    if change == "outside":
        path = home.owner_home_dir / "business.log"
        path.write_bytes(b"business")
    ledger = _retire(store, path)
    protected = None
    if change == "replaced":
        replacement = path.with_name("replacement.tmp")
        replacement.write_bytes(b"new file")
        assert replacement.stat().st_ino != path.stat().st_ino
        os.replace(replacement, path)
    elif change == "symlink":
        protected = tmp_path / "protected"
        protected.write_bytes(b"protected")
        path.unlink()
        path.symlink_to(protected)
    elif change == "hardlink":
        protected = path.with_name("another-link")
        try:
            os.link(path, protected)
            request.node.user_properties.append(("hardlink_observation", "real_filesystem"))
        except PermissionError as exc:
            assert exc.errno in {errno.EPERM, errno.EACCES}
            # 本地已实测 link 被拒；不跳过门禁，改用确定性多链接元数据验证同一拒绝分支，并明确不充当物理硬链接验收。
            original_lstat = retention.os.lstat
            def multiple_links(value, *args, **kwargs):
                info = original_lstat(value, *args, **kwargs)
                if Path(value) == path:
                    fields = list(info)
                    fields[3] = 2
                    return os.stat_result(fields)
                return info
            monkeypatch.setattr(retention.os, "lstat", multiple_links)
            request.node.user_properties.append(("hardlink_observation", "EPERM_or_EACCES_stat_double_only"))
            protected = None
    elif change == "parent_symlink":
        moved = tmp_path / "moved-jobs"
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
        protected = moved / path.name
    elif change == "missing":
        path.unlink()
    commits = []
    results = _finish(home, commit=lambda op: (commits.append("write"), op())[1])
    assert sum(row["skipped"].get("log_identity_changed", 0) for row in results) == 1
    assert results[-1]["cursor"]["log_offset"] == ledger.stat().st_size
    assert not commits  # 身份不匹配只能结束行，不触发任何变更批次。
    assert path.exists() is (change != "missing")
    if protected:
        assert protected.exists()


def test_consume_cleanup_removes_sidecar_and_persists_log(domain):
    home, store = domain
    path = _log(home)
    row = _activation(output_file=str(path), stop_requested=True, termination={"cleanup": {"confirmed": True,
        "instances": [{"method": "fixture", "confirmed": True, "return_code": 0,
                       "observed_processes": 1, "unresolved_pids": []}]}})
    saved = store.write(row)
    reference = process_cleanup_reference(saved)
    sidecar = store.root / "bg-a.json.lock"
    sidecar.touch()
    with store.transaction() as transaction:
        transaction.consume_cleanup((reference,))
        transaction.consume_cleanup((reference,))
    assert not store.record_path("bg-a").exists() and not sidecar.exists()
    assert json.loads((store.root / ".log_retirement.jsonl").read_text())["session_id"] == "bg-a"
    assert path.exists()


def test_legacy_launch_locks_old_only_and_directory_lock_is_never_removed(domain):
    home, store = domain
    store.write(_running(output_file=""))
    directory = store.root / ".launches"
    directory.mkdir()
    old, young = directory / "bg-old.json.lock", directory / "bg-young.json.lock"
    backed = directory / "bg-backed.json.lock"
    for path in (old, young, backed):
        path.touch()
    (directory / "bg-backed.json").write_text("{}")
    orphan = store.root / "bg-old.json.lock"
    orphan.touch()
    for path in (old, backed, orphan, store.root / ".process-sessions.lock"):
        os.utime(path, (NOW - 2 * DAY, NOW - 2 * DAY))
    os.utime(young, (NOW - 3600, NOW - 3600))
    results = _finish(home)
    assert not old.exists() and not orphan.exists()
    assert young.exists() and backed.exists() and (store.root / ".process-sessions.lock").exists()
    assert sum(row["deleted_items"] for row in results) == 2
    assert sum(row["backlog"] for row in results) == 2


def test_compaction_keeps_unconsumed_tail_byte_identical(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    consumed = ledger.read_bytes()
    tail_path = _log(home, "job-2.log")
    _retire(store, tail_path, session="bg-future", finished=NOW - 2 * DAY)
    tail = ledger.read_bytes()[len(consumed):]
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", len(consumed))
    result = _finish(home)[-1]
    assert not path.exists() and tail_path.exists()
    assert ledger.read_bytes() == tail and result["cursor"]["log_offset"] == 0
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert not list(store.root.glob(".log_retirement-*.tmp"))


@pytest.mark.parametrize("legal_hold", [False, True])
def test_empty_owner_has_zero_commits_and_does_not_create_store(domain, legal_hold):
    home, store = domain
    commits = []
    request = _request(home, commit=lambda op: (commits.append(op), op())[1], legal_hold=legal_hold)
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(request))
    assert result["complete"] and result["deleted_items"] == result["freed_bytes"] == 0
    assert not commits and not store.root.exists()
    if legal_hold:
        assert result["deferred_reason"] == "legal_hold"


def test_legal_hold_protects_existing_retired_log(domain):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    before = ledger.read_bytes()
    result = retention.maintain_process_sessions_lane(_request(home, legal_hold=True))
    assert result["complete"] and path.exists() and ledger.read_bytes() == before


def test_expired_tail_only_adds_fact_for_missing_canonical_job(tmp_path):
    expected = registry._read_log_tail(str(tmp_path / ".background_jobs" / "job-1.log"), 100)
    assert expected.expired and expected.to_fact()["expired"] is True
    for path in (tmp_path / "x.log", tmp_path / ".background_jobs" / "job-x.log",
                 tmp_path / "elsewhere" / "job-1.log"):
        tail = registry._read_log_tail(str(path), 100)
        assert not tail.expired and "expired" not in tail.to_fact()
        assert tail.error_type == "FileNotFoundError"


def test_unlink_error_retries_same_row_without_losing_locator(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    original = retention._unlink_log
    def fail(row):
        raise OSError("unlink unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(retention, "_unlink_log", fail)
        result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home)))
    assert result["errors"] == [{"code": "background_log_unlink_failed"}]
    assert result["cursor"]["log_offset"] == 0 and path.exists() and ledger.exists()
    assert retention._unlink_log is original
    _finish(home, cursor=result["cursor"])
    assert not path.exists()


def test_record_unlink_failure_never_authorizes_deleting_its_log(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    store.write(_running(status="exited", exit_code=0, finished_at=NOW - 8 * DAY, output_file=str(path)))
    original = store_module.unlink_file_beneath
    def fail(root, parts):
        if parts == ("bg-a.json",):
            raise OSError("metadata still retained")
        return original(root, parts)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "unlink_file_beneath", fail)
        with pytest.raises(OSError):
            store.prune_finished(max_age_seconds=DAY, now=NOW)
    result = retention.maintain_process_sessions_lane(_request(home, cursor={"phase": "logs"}))
    assert result["skipped"]["session_record_present"] == 1 and result["cursor"]["log_offset"] == 0
    assert path.exists()
    assert store.prune_finished(max_age_seconds=DAY, now=NOW) == ["bg-a"]
    _finish(home)
    assert not path.exists()


def test_partial_tail_and_future_head_are_not_consumed(domain):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path, finished=NOW - 2 * DAY)
    future = ledger.read_bytes()
    due = _log(home, "job-2.log")
    _retire(store, due, session="bg-due")
    result = _finish(home)[-1]
    assert result["cursor"]["log_offset"] == 0 and path.exists() and due.exists()
    ledger.write_bytes(future.rstrip(b"\n"))
    result = _finish(home, now=NOW + 10 * DAY)[-1]
    assert result["cursor"]["log_offset"] == 0 and path.exists()


def test_item_and_byte_budgets_bound_log_work(domain):
    home, store = domain
    paths = [_log(home, f"job-{index}.log") for index in range(3)]
    for index, path in enumerate(paths):
        _retire(store, path, session=f"bg-{index}")
    request = _request(home, cursor={"phase": "logs"}, budget=StorageLaneBudget(1, 10000, None))
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(request))
    assert result["items"] == 1 and result["deleted_items"] == 1 and not result["complete"]
    assert sum(path.exists() for path in paths) == 2
    _finish(home, cursor=result["cursor"], budget=StorageLaneBudget(1, 10000, None))
    assert not any(path.exists() for path in paths)


def test_exhausted_budget_never_commits(domain):
    home, store = domain
    path = _log(home)
    _retire(store, path)
    commits = []
    for budget in (StorageLaneBudget(0, None, None), StorageLaneBudget(None, 0, None),
                   StorageLaneBudget(None, None, time.monotonic() - 1)):
        result = retention.maintain_process_sessions_lane(_request(home, budget=budget,
            commit=lambda op: (commits.append(op), op())[1]))
        assert not result["complete"] and result["items"] == 0
    assert not commits and path.exists()


def test_busy_store_lock_is_bounded_and_retryable(domain):
    home, store = domain
    store.write(_running(output_file=""))
    ready, release = threading.Event(), threading.Event()
    def holder():
        with store.transaction():
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(5)
        result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home)))
        assert result["skipped"]["lock_busy"] == 1 and not result["complete"]
        assert not result["errors"]
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    _finish(home)


@pytest.mark.parametrize("damage", ["schema", "time", "json"])
def test_unknown_ledger_rows_keep_file_and_offset(domain, damage):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    row = json.loads(ledger.read_text())
    if damage == "schema":
        row["schema"] = "future_schema.v9"
    elif damage == "time":
        row["finished_at"] = "unparseable"
    ledger.write_bytes(b"{\n" if damage == "json" else json.dumps(row).encode() + b"\n")
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home, cursor={"phase": "logs"})))
    assert result["skipped"]["log_state_unknown"] == 1
    assert result["cursor"]["log_offset"] == 0 and path.exists() and not result["complete"]


@pytest.mark.parametrize("surface", ["root", "ledger"])
def test_symlinked_store_or_ledger_is_never_followed(domain, tmp_path, surface):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    if surface == "root":
        destination = tmp_path / "external-store"
        store.root.rename(destination)
        store.root.symlink_to(destination, target_is_directory=True)
    else:
        destination = tmp_path / "external-ledger"
        ledger.rename(destination)
        ledger.symlink_to(destination)
    before = (destination / ".log_retirement.jsonl").read_bytes() if surface == "root" else destination.read_bytes()
    commits = []
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home, cursor={"phase": "logs"},
        commit=lambda op: (commits.append(op), op())[1])))
    assert not commits and not result["complete"] and path.exists()
    assert result["skipped"]
    after = (destination / ".log_retirement.jsonl").read_bytes() if surface == "root" else destination.read_bytes()
    assert before == after


def test_untracked_job_is_not_scanned_or_deleted(domain):
    home, store = domain
    store.write(_running(output_file=""))
    untracked = _log(home)
    old = NOW - 100 * DAY
    os.utime(untracked, (old, old))
    commits = []
    _finish(home, commit=lambda op: (commits.append(op), op())[1])
    assert untracked.exists() and not commits


def test_compaction_resumes_after_one_item_budget(domain, monkeypatch):
    home, store = domain
    old = _log(home)
    ledger = _retire(store, old)
    consumed = ledger.stat().st_size
    future = _log(home, "job-2.log")
    _retire(store, future, session="bg-future", finished=NOW - 2 * DAY)
    expected_tail = ledger.read_bytes()[consumed:]
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", consumed)
    results = _finish(home, cursor={"phase": "logs"}, budget=StorageLaneBudget(1, 10000, None))
    assert not old.exists() and future.exists()
    assert ledger.read_bytes() == expected_tail
    assert results[-1]["cursor"]["log_offset"] == 0
    assert all(row["items"] <= 1 for row in results)


def test_compaction_reloads_tail_under_same_store_lock(domain, monkeypatch):
    home, store = domain
    old = _log(home)
    ledger = _retire(store, old)
    cursor = _finish(home)[-1]["cursor"]
    prefix = ledger.stat().st_size
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", prefix)
    future = _log(home, "job-2.log")
    expected = []
    def commit(operation):
        _retire(store, future, session="bg-future", finished=NOW - 2 * DAY)
        expected.append(ledger.read_bytes()[prefix:])
        return operation()
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home,
        cursor={**cursor, "phase": cursor["phase"].replace("prune", "compact", 1)}, commit=commit)))
    assert result["complete"] and ledger.read_bytes() == expected[0]
    assert future.exists() and result["cursor"]["log_offset"] == 0


def test_compaction_shrunk_ledger_aborts_without_commit(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    offset = ledger.stat().st_size + 1
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", 1)
    commits = []
    identity = ledger.stat()
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home,
        cursor={"phase": f"compact@{identity.st_dev}:{identity.st_ino}", "log_offset": offset},
        commit=lambda op: (commits.append(op), op())[1])))
    assert not result["complete"] and result["skipped"]["ledger_shrank"] == 1
    assert result["cursor"]["log_offset"] == 0 and path.exists() and not commits


def test_byte_budget_does_not_consume_partial_line(domain):
    home, store = domain
    path = _log(home)
    ledger = _retire(store, path)
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home,
        cursor={"phase": "logs"}, budget=StorageLaneBudget(None, ledger.stat().st_size - 1, None))))
    assert result["bytes"] == ledger.stat().st_size - 1 and result["items"] == 0
    assert result["cursor"]["log_offset"] == 0 and not result["complete"] and path.exists()
    _finish(home, cursor=result["cursor"])
    assert not path.exists()


def test_lane_keeps_completion_targets_without_resolver(domain):
    home, store = domain
    target = {"store_root": "/conversations", "thread_id": "thread-a", "task_id": "task-a", "run_id": "run-a"}
    path = _log(home)
    store.write(_running(status="exited", finished_at=NOW - 2 * DAY, exit_code=0,
                         output_file=str(path), completion_target=target, completion_notice_id="notice-consumed"))
    commits = []
    _finish(home, commit=lambda op: (commits.append(op), op())[1])
    assert store.load("bg-a").record and path.exists() and not commits


def test_log_unlink_supports_no_dir_fd_platform(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    _retire(store, path)
    monkeypatch.setattr(retention.os, "supports_dir_fd", set())
    _finish(home)
    assert not path.exists()


def test_lock_sweep_catches_up_10000_legacy_specs_in_one_batch(domain):
    home, store = domain
    store.write(_running(output_file=""))
    directory = store.root / ".launches"
    directory.mkdir()
    for index in range(10000):
        path = directory / f"bg-{index}.json.lock"
        path.touch()
        os.utime(path, (NOW - 2 * DAY, NOW - 2 * DAY))
    commits = []
    results = _finish(home, commit=lambda op: (commits.append("write"), op())[1])
    assert not list(directory.glob("*.lock"))
    assert sum(row["deleted_items"] for row in results) == 10000
    assert sum(row["backlog"] for row in results) == 10000
    assert commits == ["write"]


@pytest.mark.parametrize("cursor", [{"log_offset": -1}, {"phase": "unknown"}, {"next_prune_at": float("nan")}])
def test_invalid_cursor_cannot_authorize_deletion(domain, cursor):
    home, store = domain
    path = _log(home)
    _retire(store, path)
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home, cursor=cursor)))
    assert result["skipped"]["maintenance_input_unknown"] == 1 and path.exists()


def test_nonfinite_wall_clock_cannot_expire_logs(domain):
    home, store = domain
    path = _log(home)
    _retire(store, path)
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home, now=float("nan"))))
    assert result["skipped"]["maintenance_input_unknown"] == 1 and path.exists()


def test_old_cursor_after_other_compaction_restarts_before_unconsumed_rows(domain, monkeypatch):
    home, store = domain
    old = _log(home)
    ledger = _retire(store, old)
    prefix = ledger.stat().st_size
    future = _log(home, "job-22222222222222222222.log")
    _retire(store, future, session="bg-future-with-longer-name", finished=NOW - 2 * DAY)
    request = _request(home, cursor={"phase": "logs"}, budget=StorageLaneBudget(1, 10000, None))
    stale = retention.maintain_process_sessions_lane(request)["cursor"]
    assert stale["log_offset"] == prefix and "@" in stale["phase"]
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", prefix)
    compact_cursor = {**stale, "phase": stale["phase"].replace("logs", "compact", 1)}
    compacted = _finish(home, cursor=compact_cursor)[-1]
    assert compacted["cursor"]["log_offset"] == 0 and future.exists()
    assert ledger.stat().st_size >= prefix  # 长度检查不能发现这次换代。
    results = _finish(home, cursor=stale, now=NOW + 8 * DAY)
    assert not future.exists()
    assert sum(row["skipped"].get("ledger_changed", 0) for row in results) >= 1


def test_unlink_parent_sync_failure_keeps_offset_until_retry(domain, monkeypatch):
    home, store = domain
    path = _log(home)
    _retire(store, path)
    original = retention.os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync unavailable")
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(retention.os, "fsync", fail_directory)
        result = retention.maintain_process_sessions_lane(_request(home, cursor={"phase": "logs"}))
        if os.name == "nt":
            # Windows 无目录 fsync，但同一真实删除入口仍必须删除日志并完成，不能仅以平台标签冒充通过。
            assert result["complete"] and not result["errors"] and not path.exists()
            return
        assert result["cursor"]["log_offset"] == 0
        assert result["errors"] == [{"code": "background_log_unlink_failed"}]
        assert not path.exists()
        again = retention.maintain_process_sessions_lane(_request(home, cursor=result["cursor"]))
        assert again["cursor"]["log_offset"] == 0 and again["errors"]
    _finish(home, cursor=result["cursor"])


def test_oversize_compaction_tail_does_not_starve_logs(domain, monkeypatch):
    home, store = domain
    old = _log(home)
    ledger = _retire(store, old)
    _finish(home)
    prefix = ledger.stat().st_size
    future = _log(home, "job-2.log")
    _retire(store, future, session="bg-future", finished=NOW - 2 * DAY)
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", prefix)
    tiny = StorageLaneBudget(128, 10, None)
    identity = ledger.stat()
    result = validate_lane_receipt(retention.maintain_process_sessions_lane(_request(home,
        cursor={"phase": f"compact@{identity.st_dev}:{identity.st_ino}", "log_offset": prefix}, budget=tiny)))
    assert result["cursor"]["phase"].split("@", 1)[0] == "logs"
    _finish(home, cursor=result["cursor"], now=NOW + 8 * DAY)
    assert not future.exists()


@pytest.mark.parametrize("phase", ["logs", "compact"])
def test_unbound_legacy_offset_replays_instead_of_discarding_retirement_rows(domain, monkeypatch, phase):
    home, store = domain
    first = _log(home)
    ledger = _retire(store, first)
    prefix = ledger.stat().st_size
    second = _log(home, "job-2222222222222222.log")
    _retire(store, second, session="bg-second-long-session")
    assert ledger.stat().st_size >= prefix
    monkeypatch.setattr(retention, "BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES", prefix)
    results = _finish(home, cursor={"phase": phase, "log_offset": prefix})
    assert not first.exists() and not second.exists()
    assert sum(row["deleted_items"] for row in results) == 2
    assert sum(row["skipped"].get("ledger_generation_unknown", 0) for row in results) == 1
