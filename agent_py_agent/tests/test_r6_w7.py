"""真实损坏副本上的恢复、代次排空、持久阶段与 Gateway 启动回归。"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import (
    SALVAGE_REQUEST_SUFFIX,
    note_runtime_db_corruption,
    runtime_db_identity,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.cli import gateway_process
from agent_py_agent.tests import test_gap_w1_wire_roundtrip as roundtrip
from agent_py_agent.tests.test_rdb_w3_runtime_db_salvage import _damaged_copy, _source

wire = roundtrip.wire


def _blocked(path):
    with pytest.raises(guard.RuntimeDatabaseDeferred):
        with guard.runtime_db_generation_guard(path, exclusive=True):
            pytest.fail("物理连接仍存活，不能放行换库")


@pytest.mark.parametrize("mode", ["existing_only", "transaction", "read_only", "source"])
def test_real_runtime_connections_block_ex_until_physical_close(tmp_path, monkeypatch, mode):
    source = _source(tmp_path / "runtime.db")
    if mode == "source":
        original = salvage._copy_table

        def copy(conn, table):
            _blocked(source)
            return original(conn, table)

        monkeypatch.setattr(salvage, "_copy_table", copy)
        assert salvage.salvage_runtime_db(source, tmp_path / "candidate.db").ok
    else:
        repo = RuntimeRepository(source, read_only=mode == "read_only", existing_only=mode == "existing_only")
        context = repo.transaction() if mode == "transaction" else repo._runtime_connection()
        with context:
            _blocked(source)
    with guard.runtime_db_generation_guard(source, exclusive=True):
        pass


@pytest.fixture
def damaged(tmp_path):
    source = _source(tmp_path / "source/runtime.db", events=1024)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE tool_operations SET status='UNKNOWN'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return _damaged_copy(source, tmp_path / "owner/runtime.db", table="runtime_events", tail=True)


def _request(db):
    return Path(f"{db}{SALVAGE_REQUEST_SUFFIX}")


def _assert_authority(db, watermark=1024):
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM tool_operations").fetchone()[0] == "UNKNOWN"
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='runtime_events'").fetchone()[0] >= watermark


def test_startup_heals_marker_and_retains_raw_backup(damaged):
    before = damaged.read_bytes()
    identity = runtime_db_identity(damaged)
    _request(damaged).touch()
    receipt = salvage.heal_runtime_db_at_startup(damaged)
    assert receipt.ok and receipt.events_error and 0 < receipt.events_copied < 1024
    assert runtime_db_identity(damaged) != identity and not _request(damaged).exists()
    report = json.loads(Path(f"{damaged}.recovery-result.json").read_text())
    assert damaged.with_name(report["backup"]).read_bytes() == before
    assert report["events_loss"] is True
    _assert_authority(damaged)


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("transaction", [False, True])
def test_online_failure_is_not_replayed_and_next_query_succeeds(damaged, reuse, transaction):
    with sqlite_connection_scope(reuse):
        repo = RuntimeRepository(damaged)
        entered = []
        with pytest.raises(sqlite3.DatabaseError) as caught:
            with (repo.transaction() if transaction else repo._runtime_connection()) as conn:
                entered.append(True)
                conn.execute("SELECT * FROM runtime_events").fetchall()
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
        assert caught.value.runtime_db_recovery["status"] == "recovered"
        assert entered == [True]
        with repo._runtime_connection() as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    _assert_authority(damaged)


def test_no_request_does_not_open_database(damaged, monkeypatch):
    monkeypatch.setattr(salvage.sqlite3, "connect", lambda *a, **kw: pytest.fail("无请求不能扫描库"))
    assert salvage.heal_runtime_db_at_startup(damaged) is None


@pytest.mark.parametrize("limited", ["read_only", "existing_only"])
def test_readers_report_corruption_without_recovery_writes(damaged, monkeypatch, limited):
    repo = RuntimeRepository(damaged, **{limited: True})
    monkeypatch.setattr(salvage, "recover_runtime_database", lambda *a, **kw: pytest.fail("只读探针不能获恢复权"))
    with sqlite_connection_scope(True), pytest.raises(sqlite3.DatabaseError) as caught:
        with repo._runtime_connection() as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    assert caught.value.runtime_db_recovery["status"] == "requires_recovery"
    assert not _request(damaged).exists() and not guard.runtime_db_recovery_pending(damaged)


@pytest.mark.parametrize("table", [None, "tasks"])
def test_unverifiable_core_is_retained_and_deduplicated(tmp_path, monkeypatch, table):
    source = _source(tmp_path / "source/runtime.db")
    if table:
        db = _damaged_copy(source, tmp_path / "owner/runtime.db", table=table)
    else:
        db = source
        with db.open("r+b") as stream:
            stream.write(b"not a sqlite header!")
    identity, raw = runtime_db_identity(db), db.read_bytes()
    _request(db).touch()
    first = salvage.recover_runtime_database(db, expected_identity=identity)
    assert first["status"] == "requires_recovery", first
    assert db.with_name(first["candidate"]).exists()
    monkeypatch.setattr(salvage, "salvage_runtime_db", lambda *a, **kw: pytest.fail("同源不可重复抢救"))
    second = salvage.recover_runtime_database(db, expected_identity=identity)
    assert second["status"] == "requires_recovery"
    assert db.read_bytes() == raw and runtime_db_identity(db) == identity and _request(db).exists()
    healthy = RuntimeRepository(tmp_path / "healthy/runtime.db")
    with healthy._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("error", [sqlite3.OperationalError("no such table"), PermissionError("denied")])
def test_noncorruption_never_requests_salvage(tmp_path, monkeypatch, error):
    db = _source(tmp_path / "runtime.db")
    repo = RuntimeRepository(db)

    @contextmanager
    def failing():
        raise error
        yield

    monkeypatch.setattr(repo, "_runtime_connection_scope", failing)
    monkeypatch.setattr(salvage, "recover_runtime_database", lambda *a, **kw: pytest.fail("错误分类越权"))
    with pytest.raises(type(error)) as caught:
        with repo._runtime_connection():
            pass
    assert caught.value is error and not _request(db).exists()


def test_gateway_startup_hook_uses_owner_and_defers(tmp_path, monkeypatch, capsys):
    seen = []

    def failing(path):
        seen.append(Path(path))
        raise BlockingIOError("attached")

    monkeypatch.setattr(salvage, "heal_runtime_db_at_startup", failing)
    args = SimpleNamespace(config=None, _gateway_frozen_config=AgentConfig(my_agent_home=str(tmp_path / "home")))
    gateway_process._heal_runtime_db_before_agent(args)
    assert seen == [tmp_path / "home/owners/local/main/runtime.db"]
    assert "runtime_db_salvage_deferred error=BlockingIOError" in capsys.readouterr().err


def test_transient_wal_signal_waits_for_physical_close_then_clears(tmp_path, damaged):
    db = _source(tmp_path / "healthy/runtime.db")
    repo = RuntimeRepository(db)
    identity = runtime_db_identity(db)
    with pytest.raises(sqlite3.DatabaseError) as actual:
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    with repo._runtime_connection() as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES('wal-state','preserve')")
        conn.commit()
        note_runtime_db_corruption(db, actual.value)
        report = salvage.recover_runtime_database(db, expected_identity=identity)
        assert report["status"] == "deferred" and _request(db).exists()
        assert runtime_db_identity(db) == identity
    assert salvage.heal_runtime_db_at_startup(db) is None
    assert runtime_db_identity(db) == identity and not _request(db).exists()
    assert not list(db.parent.glob("*.damaged-*")) and not list(db.parent.glob("*.salvage-*"))
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='wal-state'").fetchone()[0] == "preserve"
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='runtime_events'").fetchone()[0] == 11


def test_two_threads_only_publish_once(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    entered, release = threading.Event(), threading.Event()
    original = salvage._copy_backup
    results = []

    def held(*args):
        entered.set()
        assert release.wait(15)
        return original(*args)

    monkeypatch.setattr(salvage, "_copy_backup", held)
    worker = threading.Thread(target=lambda: results.append(salvage.recover_runtime_database(damaged, expected_identity=identity)))
    worker.start()
    try:
        assert entered.wait(15)
        second = salvage.recover_runtime_database(damaged, expected_identity=identity)
        assert second["status"] == "deferred"
        assert runtime_db_identity(damaged) == identity
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and results[0]["status"] == "recovered", results
    assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "already_recovered"
    assert len([p for p in damaged.parent.glob("*.damaged-*") if not p.name.endswith(("-wal", "-shm", "-journal"))]) == 1
    _assert_authority(damaged)


def _child(code, db, *arguments):
    return subprocess.Popen([sys.executable, "-c", code, str(db), *arguments],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _stop_child(child):
    if child.stdin and not child.stdin.closed:
        child.stdin.close()
        child.stdin = None
    output, error = child.communicate(timeout=30)
    assert child.returncode == 0, (output, error)
    return output


@pytest.mark.parametrize("managed", [False, True])
def test_idle_reader_in_other_process_defers_swap(damaged, managed):
    identity = runtime_db_identity(damaged)
    code = """
import sqlite3, sys
from pathlib import Path
from contextlib import closing
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
db = Path(sys.argv[1])
context = RuntimeRepository(db, read_only=True)._runtime_connection() if sys.argv[2] == 'managed' else closing(sqlite3.connect(db))
with context as conn:
    conn.execute('SELECT count(*) FROM sqlite_master').fetchall()
    print('attached', flush=True)
    sys.stdin.read()
"""
    holder = _child(code, damaged, "managed" if managed else "raw")
    try:
        assert holder.stdout.readline().strip() == "attached"
        result = salvage.recover_runtime_database(damaged, expected_identity=identity)
        assert result["status"] == "deferred", result
        assert runtime_db_identity(damaged) == identity
        assert not list(damaged.parent.glob("*.damaged-*"))
    finally:
        _stop_child(holder)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered", result
    _assert_authority(damaged)


def test_dispatch_child_leaves_durable_request_for_host(damaged):
    identity = runtime_db_identity(damaged)
    with RuntimeRepository(damaged)._runtime_connection() as holder:
        holder.execute("SELECT count(*) FROM tasks").fetchall()
        child = _child("""
import sqlite3, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
try:
    with RuntimeRepository(Path(sys.argv[1]))._runtime_connection() as conn:
        conn.execute('SELECT * FROM runtime_events').fetchall()
except sqlite3.DatabaseError as error:
    assert error.runtime_db_recovery['status'] == 'deferred'
else:
    raise AssertionError('expected real corruption')
""", damaged)
        _stop_child(child)
        assert _request(damaged).is_file() and runtime_db_identity(damaged) == identity
    with RuntimeRepository(damaged)._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert runtime_db_identity(damaged) != identity
    _assert_authority(damaged)


@pytest.mark.parametrize("phase", ["backing_up", "backed_up", "building_candidate", "candidate_ready", "publishing", "published"])
def test_process_exit_at_durable_phase_resumes(damaged, phase):
    identity = runtime_db_identity(damaged)
    child = _child("""
import os, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.schema import runtime_db_identity
db, phase = Path(sys.argv[1]), sys.argv[2]
original = salvage._save_state
def crash(db, state, stage):
    original(db, state, stage)
    if stage == phase:
        os._exit(73)
salvage._save_state = crash
salvage.recover_runtime_database(db, expected_identity=runtime_db_identity(db))
raise AssertionError('did not reach crash boundary')
""", damaged, phase)
    output, error = child.communicate(timeout=30)
    assert child.returncode == 73, (output, error)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered", result
    assert not _request(damaged).exists() and not guard.runtime_db_recovery_pending(damaged)
    _assert_authority(damaged)


def test_single_publish_rename_failure_keeps_journal_and_resumes(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    replace = salvage.os.replace
    failures = []

    def fail_once(source, destination):
        if Path(destination) == damaged and not failures:
            failures.append(True)
            raise PermissionError("single rename failure")
        return replace(source, destination)

    monkeypatch.setattr(salvage.os, "replace", fail_once)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "deferred" and result["phase"] == "publishing", result
    assert runtime_db_identity(damaged) == identity and guard.runtime_db_recovery_pending(damaged)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered" and failures == [True], result
    _assert_authority(damaged)


def test_new_generation_is_never_overwritten(damaged, tmp_path, monkeypatch):
    identity = runtime_db_identity(damaged)
    replacement = _source(tmp_path / "new/runtime.db")
    new_bytes = replacement.read_bytes()
    publish = salvage._publish_candidate

    def moved(db, state):
        replacement.replace(db)
        return publish(db, state)

    monkeypatch.setattr(salvage, "_publish_candidate", moved)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "superseded", result
    assert damaged.read_bytes() == new_bytes


def test_candidate_generation_failure_preserves_source_and_candidate(damaged, monkeypatch):
    identity, before = runtime_db_identity(damaged), damaged.read_bytes()
    original = salvage.salvage_runtime_db

    def newer(*args, **kwargs):
        receipt = original(*args, **kwargs)
        with closing(sqlite3.connect(receipt.target)) as conn:
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','999999')")
            conn.commit()
        return receipt

    monkeypatch.setattr(salvage, "salvage_runtime_db", newer)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "requires_recovery", result
    assert runtime_db_identity(damaged) == identity and damaged.read_bytes() == before
    assert damaged.with_name(result["candidate"]).exists()


def test_two_processes_only_publish_once(damaged):
    identity = runtime_db_identity(damaged)
    child = _child("""
import json, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.schema import runtime_db_identity
db = Path(sys.argv[1])
original = salvage._copy_backup
def held(*args):
    print('exclusive', flush=True)
    sys.stdin.readline()
    return original(*args)
salvage._copy_backup = held
print(json.dumps(salvage.recover_runtime_database(db, expected_identity=runtime_db_identity(db))), flush=True)
""", damaged)
    try:
        assert child.stdout.readline().strip() == "exclusive"
        assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "deferred"
    finally:
        output = _stop_child(child)
    assert json.loads(output)["status"] == "recovered"
    assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "already_recovered"
    assert len([p for p in damaged.parent.glob("*.damaged-*") if not p.name.endswith(("-wal", "-shm", "-journal"))]) == 1
    _assert_authority(damaged)


def test_real_client_gateway_envelope_then_recovered_request(wire, damaged, monkeypatch):
    from agent_py_agent.agent.settings import model_scope

    repo = RuntimeRepository(damaged)
    entered = []

    def real_database_call():
        entered.append(True)
        with repo._runtime_connection() as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()

    original = model_scope._profile_backend

    def backend(host, config):
        host.backend_hook = real_database_call
        return original(host, config)

    monkeypatch.setattr(model_scope, "_profile_backend", backend)
    status, _, raw = roundtrip.request(wire, "/api/llm/test", {})
    envelope = json.loads(raw)
    assert status == 500 and envelope["error"]["code"] == "HOST_NOT_READY", envelope
    assert entered == [True]
    status, _, raw = roundtrip.request(wire, "/api/llm/test", {})
    envelope = json.loads(raw)
    assert status == 200 and envelope["ok"] is True and "error" not in envelope, envelope
    assert entered == [True, True]
    _assert_authority(damaged)


def test_gateway_startup_hook_really_heals_before_construction(damaged, tmp_path, capsys):
    home = tmp_path / "gateway-home"
    db = home / "owners/local/main/runtime.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(damaged.read_bytes())
    identity = runtime_db_identity(db)
    _request(db).touch()
    args = SimpleNamespace(config=None, _gateway_frozen_config=AgentConfig(my_agent_home=str(home)))
    gateway_process._heal_runtime_db_before_agent(args)
    assert "runtime_db_salvaged" in capsys.readouterr().err
    assert runtime_db_identity(db) != identity and not _request(db).exists()
    RuntimeRepository(db)
    _assert_authority(db)


def test_startup_with_destroyed_event_root_preserves_authority(tmp_path):
    source = _source(tmp_path / "source/runtime.db", events=1024)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE tool_operations SET status='UNKNOWN'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = _damaged_copy(source, tmp_path / "owner/runtime.db", table="runtime_events")
    _request(db).touch()
    receipt = salvage.heal_runtime_db_at_startup(db)
    assert receipt.ok and receipt.events_copied == 0 and receipt.events_error
    _assert_authority(db)


def test_actual_busy_is_not_corruption(tmp_path):
    db = _source(tmp_path / "runtime.db")
    with closing(sqlite3.connect(db)) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            with RuntimeRepository(db, existing_only=True).transaction() as conn:
                conn.execute("UPDATE tasks SET goal='busy'")
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert not _request(db).exists() and not hasattr(caught.value, "runtime_db_recovery")


def test_failed_physical_close_defers_until_creator_can_close(tmp_path, monkeypatch):
    db = _source(tmp_path / "runtime.db")
    _request(db).touch()
    identity = runtime_db_identity(db)
    connect = sqlite3.connect
    faults = []

    class CloseOnce(sqlite3.Connection):
        def close(self):
            if not faults:
                faults.append(True)
                raise sqlite3.OperationalError("single close failure")
            return super().close()

    def traced(*args, **kwargs):
        kwargs["factory"] = CloseOnce
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)
    before = len(os.listdir("/dev/fd")) if os.name != "nt" else None
    first = salvage.recover_runtime_database(db, expected_identity=identity)
    assert first["status"] == "deferred" and guard.runtime_db_recovery_pending(db)
    assert runtime_db_identity(db) == identity
    second = salvage.recover_runtime_database(db, expected_identity=identity)
    assert second["status"] == "healthy" and faults == [True], second
    if before is not None:
        assert len(os.listdir("/dev/fd")) - before == 0


def test_retained_raw_wal_bytes_match_backup_before_salvage(damaged, monkeypatch):
    writer = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE metadata SET value='cold-wal-authority' WHERE key='fixture-marker'")
conn.commit()
os._exit(0)
""", str(damaged)], capture_output=True, text=True, timeout=30)
    assert writer.returncode == 0, writer.stderr
    original_wal = Path(f"{damaged}-wal").read_bytes()
    assert original_wal
    original = salvage.salvage_runtime_db
    saved = []

    def verify(source, target, **kwargs):
        state = json.loads(_request(source).read_text())
        backup = source.with_name(state["backup"])
        for suffix in ("", "-wal", "-shm", "-journal"):
            hot = Path(f"{source}{suffix}")
            if hot.exists():
                assert Path(f"{backup}{suffix}").read_bytes() == hot.read_bytes()
                saved.append(suffix)
        assert Path(f"{backup}-wal").read_bytes() == original_wal
        return original(source, target, **kwargs)

    monkeypatch.setattr(salvage, "salvage_runtime_db", verify)
    result = salvage.recover_runtime_database(damaged, expected_identity=runtime_db_identity(damaged))
    assert result["status"] == "recovered" and "" in saved and "-wal" in saved, result
    with closing(sqlite3.connect(damaged)) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='fixture-marker'").fetchone()[0] == "cold-wal-authority"


def test_resume_does_not_overwrite_same_inode_new_generation(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    replace = salvage.os.replace

    def fail_publish(source, destination):
        if Path(destination) == damaged:
            raise PermissionError("single publication boundary")
        return replace(source, destination)

    monkeypatch.setattr(salvage.os, "replace", fail_publish)
    first = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert first["phase"] == "publishing" and first["status"] == "deferred"
    monkeypatch.setattr(salvage.os, "replace", replace)
    # 单次确定性交错：未受管的新代写者修改同 inode；不得用旧候选盖回。
    with closing(sqlite3.connect(damaged)) as conn:
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','2')")
        conn.commit()
    current = damaged.read_bytes()
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "deferred" and result["reason"] == "source_contents_changed", result
    assert damaged.read_bytes() == current and runtime_db_identity(damaged) == identity


def test_failed_candidate_close_retires_before_next_attempt(damaged, monkeypatch):
    connect, failures = sqlite3.connect, []

    class CandidateClose(sqlite3.Connection):
        def close(self):
            if not failures:
                failures.append(True)
                raise sqlite3.OperationalError("single candidate close failure")
            return super().close()

    def traced(address, *args, **kwargs):
        if ".salvage-" in str(address) and str(address).endswith("?mode=rw"):
            kwargs["factory"] = CandidateClose
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)
    identity = runtime_db_identity(damaged)
    before = len(os.listdir("/dev/fd")) if os.name != "nt" else None
    first = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert first["status"] == "deferred" and failures == [True], first
    assert runtime_db_identity(damaged) == identity
    second = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert second["status"] == "recovered", second
    if before is not None:
        assert len(os.listdir("/dev/fd")) - before == 0
    _assert_authority(damaged)


def test_invalid_marker_keeps_original_corruption_and_source(damaged):
    before = damaged.read_bytes()
    with pytest.raises(sqlite3.DatabaseError) as caught:
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    _request(damaged).write_text("[]")
    note_runtime_db_corruption(damaged, caught.value)
    assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert isinstance(caught.value.sqlite_recovery_error, ValueError)
    result = salvage.recover_runtime_database(damaged, expected_identity=runtime_db_identity(damaged))
    assert result["status"] == "requires_recovery"
    assert damaged.read_bytes() == before and _request(damaged).read_text() == "[]"
