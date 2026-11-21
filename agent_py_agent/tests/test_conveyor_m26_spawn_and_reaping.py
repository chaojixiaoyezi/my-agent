"""回归覆盖两个启动入口、后台会话回收及只读 redo 诊断。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt import sandbox
from agent_py_agent.agent.common import process_spawn
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.runtime_errors import RecoverableRuntimeError
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling import process_registry as registry
from agent_py_agent.agent.tooling import process_session_commit as commit
from agent_py_agent.agent.tooling import process_session_retention as retention
from agent_py_agent.agent.tooling import process_session_store as storage
from agent_py_agent.agent.tooling import shell
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.user_space import directory_paging, storage_lanes
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest

HOST_MODULE = "agent_py_agent.agent.tooling.background_process_host"
CHILD_TIMEOUT = 10
FD_PROBE = """
import json
import os

fds = []
for name in os.listdir("/dev/fd"):
    try:
        os.fstat(int(name))
    except OSError:
        continue
    fds.append(int(name))
print(json.dumps([sorted(fds), os.getpid() == os.getsid(0)]))
"""


@pytest.fixture
def native_calls(monkeypatch):
    calls = []
    spawn = process_spawn._posix_spawn
    fork_exec = getattr(subprocess, "_fork_exec", None)

    def track(path, argv, env, **options):
        calls.append(argv)
        return spawn(path, argv, env, **options)

    def reject_managed_fork(argv, *args):
        if FD_PROBE in argv or HOST_MODULE in argv:
            pytest.fail("managed launch called subprocess._fork_exec")
        return fork_exec(argv, *args)

    if sys.platform == "darwin":
        monkeypatch.setattr(process_spawn, "_posix_spawn", track)
        monkeypatch.setattr(subprocess, "_fork_exec", reject_managed_fork)
    return calls


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor and session contract")
def test_foreground_shell_uses_native_spawn_and_passes_only_selected_fds(tmp_path, monkeypatch, native_calls):
    with ExitStack() as resources:
        read_fd, write_fd = os.pipe()
        resources.callback(os.close, read_fd)
        resources.callback(os.close, write_fd)
        os.set_inheritable(read_fd, True)
        monkeypatch.setattr(sandbox, "adopt_prebuilt_landlock_ruleset", lambda argv: (argv, (write_fd,)))
        with shell._start_attempt_shell_process(
            [sys.executable, "-c", FD_PROBE], False, tmp_path, shell.ShellTool(tmp_path),
        ) as child:
            stdout, stderr = child.communicate(timeout=CHILD_TIMEOUT)
        assert (child.returncode, stderr) == (0, "")
        assert json.loads(stdout) == [[0, 1, 2, write_fd], True]
    if sys.platform == "darwin":
        assert any(FD_PROBE.encode() in argv for argv in native_calls)


@pytest.mark.skipif(os.name != "posix", reason="POSIX launch entry point")
@pytest.mark.parametrize("missing", ["cwd", "executable"])
def test_foreground_launch_preserves_popen_error(tmp_path, missing):
    absent = tmp_path / missing
    argv = [sys.executable, "-c", "pass"] if missing == "cwd" else [str(absent)]
    cwd = absent if missing == "cwd" else tmp_path
    with pytest.raises(FileNotFoundError) as stock:
        subprocess.Popen(argv, cwd=str(cwd), start_new_session=True)
    with pytest.raises(shell.SandboxUnavailable) as wrapped:
        shell._start_attempt_shell_process(argv, False, cwd, shell.ShellTool(tmp_path))
    cause = wrapped.value.__cause__
    assert type(cause) is FileNotFoundError
    assert (cause.errno, cause.filename) == (stock.value.errno, stock.value.filename)


@pytest.fixture
def authority(tmp_path):
    owner = tmp_path.resolve() / "owner"
    owner.mkdir()
    home = SimpleNamespace(owner_home_dir=owner, owner_retention_json=owner / "retention.json")
    store = storage.ProcessSessionStore(storage.process_session_store_root(owner, owner))
    request = launch.BackgroundLaunchRequest(
        argv=[sys.executable, "-c", "print('m26 background')"],
        command="m26 background",
        cwd=owner,
        log_path=owner / "output.log",
        env={"PATH": os.defpath, "HOME": os.environ["HOME"]},
        max_log_bytes=4096,
        store_root=store.root,
        access_scope=ProcessAccessScope("owner", "thread", str(owner)),
        execution_scope=ProcessExecutionScope(str(owner), "thread", "task", "run", "attempt"),
    )
    return home, store, request


@pytest.mark.skipif(sys.platform != "darwin", reason="the shared native spawn implementation uses Darwin")
def test_background_launcher_uses_native_spawn(authority, native_calls):
    _, store, request = authority
    hosted = launch.start_background_process(request)
    try:
        assert hosted.process.wait(timeout=CHILD_TIMEOUT) == 0
    finally:
        if hosted.process.poll() is None:
            hosted.process.kill()
            hosted.process.wait(timeout=CHILD_TIMEOUT)
    assert any(HOST_MODULE.encode() in argv for argv in native_calls)
    assert request.log_path.read_text() == "m26 background\n"
    assert store.load(hosted.record["session_id"]).record["status"] == "exited"


@pytest.fixture
def group_leader():
    with ExitStack() as children:
        def start(*, finished):
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                stdin=subprocess.PIPE,
                start_new_session=True,
            )

            def reap():
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=CHILD_TIMEOUT)

            children.callback(reap)
            birth = registry.capture_process_birth_token(child.pid)
            assert birth
            if finished:
                child.communicate(timeout=CHILD_TIMEOUT)
            return child.pid, birth

        yield start


def _managed_record(request, session_id, host, child, **changes):
    return {
        **launch._reservation(request, session_id),
        "pid": host[0],
        "pid_birth_token": host[1],
        "child_pid": child[0],
        "child_pid_birth_token": child[1],
        "child_launch_started": True,
        "started_at": time.time(),
        "status": "running",
        **changes,
    }


def _maintain(home, now, *, cursor=None, max_items=None, write=None):
    return retention.maintain_process_sessions_lane(StorageLaneRequest(
        "process_sessions", home, now, cursor or {}, StorageLaneBudget(max_items, None, None),
        False, write is not None, None, write,
    ))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_dead_host_marked_lost_and_pruned(authority, group_leader):
    home, store, request = authority
    sid = "bg-m26-dead"
    before = store.write(_managed_record(request, sid, group_leader(finished=True), group_leader(finished=True)))
    now = time.time()
    result = _maintain(home, now)
    assert not result["errors"]
    lost = store.load(sid).record
    assert (lost["status"], lost.get("reason"), lost["exit_code"]) == ("exited", "lost", None)
    assert lost["revision"] == before["revision"] + 1 and lost["finished_at"] == now
    assert not store.record_path(sid).exists()
    result = _maintain(home, now + 86401, cursor=result["cursor"])
    assert not result["errors"] and result["deleted_items"] == 1
    assert not store.load(sid).record


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
@pytest.mark.parametrize("host_state", ["live", "reused", "child_alive"])
def test_reaping_preserves_live_processes(authority, group_leader, host_state):
    home, store, request = authority
    host = group_leader(finished=host_state == "child_alive")
    if host_state == "reused":
        host = (host[0], "proc:previous-instance")
        assert registry._process_instance_terminated(*host)
    child = group_leader(finished=host_state != "child_alive")
    sid = "bg-m26-live"
    before = store.write(_managed_record(request, sid, host, child))
    now = time.time()
    first = _maintain(home, now)
    assert not first["errors"]
    record = store.load(sid).record
    if host_state == "child_alive":
        assert record["status"] == "unknown" and record["finished_at"] is None
        second = _maintain(home, now + 1, cursor=first["cursor"])
        assert not second["errors"] and store.load(sid).record == record
        assert not registry._process_instance_terminated(*child)
    else:
        assert record == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_reaping_rechecks_a_surviving_group_after_the_leader_exits(authority, group_leader, monkeypatch):
    home, store, request = authority
    sid = "bg-m26-group"
    host, child = group_leader(finished=True), group_leader(finished=True)
    row = store.write(_managed_record(request, sid, host, child))
    now = time.time()
    killpg = os.killpg

    def group_observation(pgid, signal_number):
        if pgid == child[0] and signal_number == 0:
            return  # 组长已经退出，但内核仍报告原进程组存在。
        return killpg(pgid, signal_number)

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", group_observation)
        first = _maintain(home, now)
    assert not first["errors"]
    assert store.load(sid).record == {**row, "status": "unknown", "revision": row["revision"] + 1}
    second = _maintain(home, now + 3601, cursor=first["cursor"])
    assert not second["errors"]
    assert store.load(sid).record["reason"] == "lost"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_reaping_does_not_apply_an_old_observation_to_a_newer_record(authority, group_leader):
    home, store, request = authority
    prior = store.write(_managed_record(
        request, "bg-m26-changed", group_leader(finished=True), group_leader(finished=True),
    ))

    def write_after_stop(operation):
        store.write({**prior, "stop_requested": True})
        return operation()

    result = _maintain(home, time.time(), write=write_after_stop)
    assert not result["errors"]
    assert store.load(prior["session_id"]).record == {
        **prior, "stop_requested": True, "revision": prior["revision"] + 1,
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_reaping_resumes_past_bad_rows_and_settles_a_host_without_a_child(authority, group_leader):
    home, store, request = authority
    sid = "bg-m26-no-child"
    pid, birth = group_leader(finished=True)
    store.write({**launch._reservation(request, sid), "pid": pid, "pid_birth_token": birth})
    (store.root / "bg-bad$.json").write_text("{")
    now = time.time()
    cursor, errors = {}, []
    for _ in range(30):
        receipt = _maintain(home, now, cursor=cursor, max_items=1)
        assert receipt["items"] <= 1
        cursor = receipt["cursor"]
        errors.extend(receipt["errors"])
        if errors and not store.record_path(sid).exists():
            break
    assert errors == [{"code": "process_session_scan_failed"}]
    assert cursor["reap_directory"]
    lost = store.load(sid).record
    assert (lost["status"], lost["reason"], lost["exit_code"]) == ("not_started", "lost", None)
    assert not store.record_path(sid).exists()
    assert store.prune_finished(0) == []  # 当前终态只剩已归档的 TTL 责任。
    assert not _maintain(home, now, cursor=cursor)["errors"]


def _lane_clock(monkeypatch):
    """Deterministic monotonic time for the slice budget; it advances only when a test charges it."""
    clock = SimpleNamespace(now=0.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(retention, "time", fake_time)
    monkeypatch.setattr(storage_lanes, "time", fake_time)
    return clock


def _without_dir_fd(monkeypatch, dir_fd):
    if not dir_fd:
        # A POSIX platform without dir_fd support: strict descriptor opening is unavailable.
        monkeypatch.setattr(os, "supports_dir_fd", set())


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptors")
def test_directory_page_pages_portably_without_dir_fd_only_when_the_caller_allows_it(tmp_path, monkeypatch):
    for index in range(5):
        (tmp_path / f"entry-{index}").write_bytes(b"")
    _without_dir_fd(monkeypatch, False)
    with pytest.raises(NoFollowPathError):
        directory_paging.directory_page(tmp_path, (), {}, 2)
    names, cursor, complete = [], {}, False
    for _ in range(10):
        page, cursor, complete = directory_paging.directory_page(tmp_path, (), cursor, 2, require_dir_fd=False)
        names.extend(page)
        if complete:
            break
    assert complete and sorted(names) == [f"entry-{index}" for index in range(5)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
@pytest.mark.parametrize("dir_fd", [True, False], ids=["dir_fd", "no_dir_fd"])
def test_reaping_advances_when_enumerating_the_directory_takes_whole_slices(authority, group_leader, monkeypatch,
                                                                            dir_fd):
    home, store, request = authority
    host, child = group_leader(finished=True), group_leader(finished=True)
    rows = {sid: store.write(_managed_record(request, sid, host, child))
            for sid in (f"bg-m26-wide-{index:02}" for index in range(40))}
    for index in range(200):
        (store.root / f"unrelated-{index:03}.txt").write_bytes(b"")
    _without_dir_fd(monkeypatch, dir_fd)
    clock = _lane_clock(monkeypatch)
    page = storage_lanes.directory_page

    def charged_page(root, parts, cursor, limit, **options):
        names, following, complete = page(root, parts, cursor, limit, **options)
        if Path(root) == store.root:
            clock.now += 0.001 * max(1, following.get("last_scanned", len(names)))  # 1 ms per entry read.
        return names, following, complete

    monkeypatch.setattr(storage_lanes, "directory_page", charged_page)
    now = time.time()
    cursor, previous, reap_slices, result = None, None, 0, {}
    for _ in range(60):
        result = _maintain(home, now, cursor=cursor)
        assert not result["errors"], result
        cursor = result["cursor"]
        if result["complete"]:
            break
        if cursor["phase"] == "reap_hosts":
            # Every slice inspects at least one entry and moves the scan position forward.
            assert result["items"] >= 1 and cursor.get("reap_directory") != previous
            previous = cursor.get("reap_directory")
            reap_slices += 1
    assert result["complete"] and reap_slices >= 5
    for sid, before in rows.items():
        lost = store.load(sid).record
        assert (lost["status"], lost["reason"]) == ("exited", "lost")
        assert lost["revision"] == before["revision"] + 1
        assert not store.record_path(sid).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_damaged_archive_row_is_reported_and_kept_while_active_rows_settle(authority, group_leader, monkeypatch):
    home, store, request = authority
    sid = "bg-m26-active"
    before = store.write(_managed_record(request, sid, group_leader(finished=True), group_leader(finished=True)))
    history = store.root / "finished"
    history.mkdir(mode=0o700)
    damaged = history / "bg-m26-damaged.json"
    damaged.write_text("{")
    _lane_clock(monkeypatch)
    now = time.time()
    first = _maintain(home, now)
    assert first["errors"] == [{"code": "process_session_scan_failed"}]
    assert first["skipped"] == {"process_state_unknown": 1}
    assert first["complete"] and first["cursor"]["next_prune_at"] == now + 3600
    lost = store.load(sid).record
    assert (lost["status"], lost["reason"], lost["exit_code"]) == ("exited", "lost", None)
    assert lost["revision"] == before["revision"] + 1
    assert damaged.read_text() == "{"
    # The archive phase is retried at its next due time: the diagnostic repeats, the damaged row
    # stays, and the expired valid archive row is pruned on its own evidence.
    second = _maintain(home, now + 86401, cursor=first["cursor"])
    assert second["errors"] == [{"code": "process_session_scan_failed"}]
    assert second["complete"] and second["deleted_items"] == 1
    assert not store.load(sid).record
    assert damaged.read_text() == "{"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group observation")
def test_legacy_reap_cursor_restarts_the_scan(authority, group_leader, monkeypatch):
    home, store, request = authority
    sid = "bg-m26-legacy"
    store.write(_managed_record(request, sid, group_leader(finished=True), group_leader(finished=True)))
    _lane_clock(monkeypatch)
    now = time.time()
    legacy = {"next_prune_at": now + 3600, "log_offset": 0, "phase": "reap_hosts", "reap_after": "bg-zzz.json"}
    result = _maintain(home, now, cursor=legacy)
    assert not result["errors"] and result["complete"]
    assert "reap_after" not in result["cursor"] and "maintenance_input_unknown" not in result["skipped"]
    assert store.load(sid).record["reason"] == "lost"


@pytest.mark.parametrize("dir_fd", [True, False], ids=["dir_fd", "no_dir_fd"])
def test_unreadable_reap_position_is_reported_and_later_phases_run(authority, monkeypatch, dir_fd):
    home, store, request = authority
    store.write(launch._reservation(request, "bg-m26-position"))
    _without_dir_fd(monkeypatch, dir_fd)
    _lane_clock(monkeypatch)
    info = store.root.stat()
    now = time.time()
    # Invalid for both the native checkpoint (offset, pending) and the portable replay count (visited).
    stale = {"directory_identity": [info.st_dev, info.st_ino], "offset": -1, "pending": "not base64",
             "visited": -1}
    cursor = {"next_prune_at": now + 3600, "log_offset": 0, "phase": "reap_hosts", "reap_directory": stale}
    result = _maintain(home, now, cursor=cursor)
    assert result["errors"] == [{"code": "process_session_scan_failed"}]
    assert result["complete"] and "reap_directory" not in result["cursor"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process identity observation")
@pytest.mark.parametrize("instance", ["host", "child"])
def test_reaping_retains_unrecognized_birth_evidence(authority, group_leader, instance):
    home, store, request = authority
    sid = "bg-m26-unconfirmed"
    host, child = group_leader(finished=True), group_leader(finished=True)
    if instance == "host":
        host = (host[0], "unobserved-birth")
    else:
        child = (child[0], "unobserved-birth")
    before = store.write(_managed_record(request, sid, host, child, status="unknown"))
    result = _maintain(home, time.time())
    assert not result["errors"]
    assert store.load(sid).record == before


def _leave_redo(store, request, monkeypatch):
    write = commit.write_json_file_atomic_unlocked

    def fail_install(path, record):
        if path == store.record_path("bg-m26-redo"):
            raise OSError("simulated record installation error")
        return write(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(commit, "write_json_file_atomic_unlocked", fail_install)
        with pytest.raises(commit.ProcessSessionCommitPendingError) as raised:
            store.request_stop(request.execution_scope)
    return raised.value.receipt.transaction_id


@pytest.mark.parametrize("when", ["before_scan", "during_scan", "cached"])
def test_passive_read_reports_redo_without_installing(authority, monkeypatch, when):
    _, store, request = authority
    sid = "bg-m26-redo"
    store.write(launch._reservation(request, sid))
    redo = store.root / ".process-sessions.redo.json"
    record = store.record_path(sid)
    original_record = record.read_bytes()
    list_records = storage.ProcessSessionTransaction.list_records
    errors, published = [], []

    def publish():
        published.append((_leave_redo(store, request, monkeypatch), redo.read_bytes()))

    def scan(transaction, **kwargs):
        result = list_records(transaction, **kwargs)
        if transaction.skip_settled_records:
            publish()
        return result

    error_report = storage.process_session_error_report

    def capture(exc, *args, **kwargs):
        errors.append(exc)
        return error_report(exc, *args, **kwargs)

    monkeypatch.setattr(storage, "process_session_error_report", capture)
    if when == "during_scan":
        monkeypatch.setattr(storage.ProcessSessionTransaction, "list_records", scan)
    elif when == "cached":
        old = time.time() - 5
        os.utime(store.root, (old, old))
        assert not store.list_pending_completion_records_report().load_errors
        stat_key = store.root.stat()
        publish()
        os.utime(store.root, ns=(stat_key.st_atime_ns, stat_key.st_mtime_ns))
        assert storage._lookup_process_records_projection(store.root) is not None
    else:
        publish()
    report = store.list_pending_completion_records_report()
    assert not report.records and len(report.load_errors) == 1
    error = report.load_errors[0]
    assert error["error_type"] == "ProcessSessionRedoPending"
    assert isinstance(errors[0], RecoverableRuntimeError)
    assert errors[0].stage == error["stage"] == "redo_pending"
    assert error["recoverable"] and error["committed"] and error["recovery_required"]
    assert error["committed_effects"] == ["redo_published"]
    assert error["read_side_effects"] == "none" and error["record_installation"] == "unknown"
    assert error["session_ids"] == [sid] and error["session_id"] == sid
    transaction_id, redo_bytes = published[0]
    assert error["transaction_id"] == transaction_id
    assert error["repair_entry_point"] == "ProcessSessionStore.transaction"
    assert redo.read_bytes() == redo_bytes and record.read_bytes() == original_record
    with store.transaction() as writer:
        assert writer.load(sid)["stop_requested"]
    assert not redo.exists()


def test_passive_read_keeps_malformed_redo_commit_facts_unknown(authority):
    _, store, request = authority
    sid = "bg-m26-previous"
    store.write(launch._reservation(request, sid))
    snapshot = store.record_path(sid).read_bytes()
    redo = store.root / ".process-sessions.redo.json"
    redo.write_bytes(b"{")
    report = store.list_pending_completion_records_report()
    assert not report.records and len(report.load_errors) == 1
    error = report.load_errors[0]
    assert error["stage"] == "redo_pending" and error["observation_error_type"] == "JSONDecodeError"
    assert error["committed"] == error["committed_effects"] == "unknown"
    assert error["recoverable"] and error["session_ids"] == []
    assert redo.read_bytes() == b"{" and store.record_path(sid).read_bytes() == snapshot
