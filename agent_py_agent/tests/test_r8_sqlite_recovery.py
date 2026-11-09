"""通用库恢复以真实SQLite验证，不将损坏、未知或缺行当作空库。"""
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.io.sqlite_recovery import recover_sqlite_database
from agent_py_agent.agent.io.sqlite_scope import scoped_connection
from agent_py_agent.agent.runtime_db.recovery_guard import (
    close_runtime_connection,
    guarded_runtime_connect,
    request_runtime_db_recovery,
    runtime_db_recovery_pending,
)


def _database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value BLOB)')
        conn.executemany('INSERT INTO records VALUES(?,?)', [(i, ('record-' + str(i)).encode()) for i in range(1, 1500)])
        conn.execute('CREATE INDEX records_value ON records(value)')
        conn.execute('PRAGMA user_version=7')
        conn.commit()


def test_false_corruption_intent_is_consumed_at_original_write_entry(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path, reason="sqlite_structural_error")
    with scoped_connection(path, False, lambda: sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
    assert not runtime_db_recovery_pending(path)
    assert not list(tmp_path.glob('local.db.damaged-*'))


def test_actual_notadb_preserves_all_evidence_and_blocks_reentry(tmp_path):
    path = tmp_path / 'run-index.sqlite3'
    path.write_bytes(b'not a sqlite database' * 100)
    files = {'': path.read_bytes(), '-wal': b'original wal', '-shm': b'original shm', '-journal': b'\0' * 8 + b'original journal'}
    for suffix, raw in files.items():
        Path(str(path) + suffix).write_bytes(raw)
    request_runtime_db_recovery(path)
    report = recover_sqlite_database(path)
    assert report['status'] == 'requires_recovery', report
    assert 'next_action' in report
    assert runtime_db_recovery_pending(path)
    backup = path.with_name(report['backup'])
    for suffix, raw in files.items():
        assert Path(str(backup) + suffix).read_bytes() == raw
    before = {p.name for p in tmp_path.iterdir()}
    assert recover_sqlite_database(path)['status'] == 'requires_recovery'
    assert before == {p.name for p in tmp_path.iterdir()}


def test_real_damaged_index_rebuilt_without_record_loss(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    with closing(sqlite3.connect(path)) as conn:
        page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='records_value'").fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
        original = conn.execute('SELECT * FROM records NOT INDEXED').fetchall()
    with path.open('r+b') as stream:
        stream.seek((page - 1) * size)
        stream.write(b'\0' * size)
    request_runtime_db_recovery(path)
    report = recover_sqlite_database(path)
    assert report['status'] == 'recovered', report
    assert not runtime_db_recovery_pending(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT * FROM records NOT INDEXED').fetchall() == original
        assert conn.execute('PRAGMA user_version').fetchone() == (7,)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    assert path.with_name(report['backup']).exists()


def test_live_generation_prevents_recovery_until_physical_close(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    entered, release = threading.Event(), threading.Event()
    def reader():
        conn = guarded_runtime_connect(path, lambda: sqlite3.connect(path))
        entered.set()
        assert release.wait(5)
        close_runtime_connection(conn)
    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(5)
    try:
        request_runtime_db_recovery(path)
        assert recover_sqlite_database(path)['status'] == 'deferred'
        assert runtime_db_recovery_pending(path)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert recover_sqlite_database(path)['status'] == 'healthy'


def test_readonly_entry_does_not_turn_intent_into_mutating_recovery(tmp_path):
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path)
    with pytest.raises(sqlite3.OperationalError):
        with scoped_connection(path, True, lambda: sqlite3.connect(path)):
            pass
    assert not list(tmp_path.glob('*.damaged-*'))


def test_interrupted_dump_retires_only_bound_partial_candidate_and_retries(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_recovery
    path = tmp_path / 'local.db'
    _database(path)
    with closing(sqlite3.connect(path)) as conn:
        page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='records_value'").fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
    with path.open('r+b') as stream:
        stream.seek((page - 1) * size)
        stream.write(b'\0' * size)
    original_source = path.read_bytes()
    original_connect = sqlite3.connect
    class InterruptedConnection(sqlite3.Connection):
        statements = 0
        def execute(self, statement, parameters=()):
            self.statements += 1
            if self.statements == 6:
                raise KeyboardInterrupt('interrupted in full dump')
            return super().execute(statement, parameters)
    def connect(database, *args, **kwargs):
        if '.salvage-' in str(database) and str(database).endswith(tuple('0123456789abcdef')):
            kwargs['factory'] = InterruptedConnection
        return original_connect(database, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_recovery.sqlite3, 'connect', connect)
        with pytest.raises(KeyboardInterrupt):
            recover_sqlite_database(path)
    import json

    from agent_py_agent.agent.runtime_db.schema import SALVAGE_REQUEST_SUFFIX
    state = json.loads(Path(str(path) + SALVAGE_REQUEST_SUFFIX).read_text())
    candidate = path.with_name(state['candidate'])
    backup = path.with_name(state['backup'])
    assert state['phase'] == 'building_candidate' and candidate.exists()
    assert path.read_bytes() == backup.read_bytes() == original_source
    receipt = recover_sqlite_database(path)
    assert receipt['status'] == 'recovered', receipt
    assert backup.read_bytes() == original_source
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def test_real_generic_structural_error_marks_only_corruption_recovery_intent(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import (
        runtime_db_corruption_recovery_pending,
    )
    path = tmp_path / 'local.db'
    path.write_bytes(b'not a sqlite database' * 100)
    factory_calls = []
    def connect():
        factory_calls.append(True)
        return sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError) as error:
        with scoped_connection(path, False, connect) as conn:
            conn.execute('SELECT name FROM sqlite_master').fetchall()
    assert error.value.sqlite_errorcode & 0xff == 26
    assert runtime_db_corruption_recovery_pending(path)
    with pytest.raises(sqlite3.OperationalError) as deferred:
        with scoped_connection(path, False, connect):
            raise AssertionError('damaged source must not be admitted')
    assert deferred.value.sqlite_recovery['status'] == 'requires_recovery'
    assert factory_calls == [True]


@pytest.mark.parametrize('replace_with_manual', [False, True])
def test_automatic_consumer_rechecks_intent_under_ex_before_unlock(tmp_path, monkeypatch, replace_with_manual):
    from agent_py_agent.agent.io import sqlite_scope
    from agent_py_agent.agent.runtime_db.recovery_guard import (
        runtime_db_corruption_recovery_pending,
        runtime_db_generation_guard,
    )
    path = tmp_path / 'local.db'
    _database(path)
    request_runtime_db_recovery(path, reason='sqlite_structural_error')
    selected, replaced = threading.Event(), threading.Event()
    def controller():
        assert selected.wait(5)
        with runtime_db_generation_guard(path, exclusive=True):
            pass
        if replace_with_manual:
            request_runtime_db_recovery(path)
        replaced.set()
    thread = threading.Thread(target=controller)
    thread.start()
    original_check = sqlite_scope.runtime_db_corruption_recovery_pending
    def paused_check(db):
        result = original_check(db)
        selected.set()
        assert replaced.wait(5)
        return result
    monkeypatch.setattr(sqlite_scope, 'runtime_db_corruption_recovery_pending', paused_check)
    factories = []
    def connect():
        factories.append(True)
        return sqlite3.connect(path)
    try:
        if replace_with_manual:
            with pytest.raises(sqlite3.OperationalError) as error:
                with scoped_connection(path, False, connect):
                    raise AssertionError('new manual intent must stay fenced')
            assert error.value.sqlite_recovery['reason'] == 'recovery_intent_requires_original_controller'
            assert runtime_db_recovery_pending(path) and not runtime_db_corruption_recovery_pending(path)
            assert factories == []
        else:
            with scoped_connection(path, False, connect) as conn:
                assert conn.execute('SELECT count(*) FROM records').fetchone() == (1499,)
            assert not runtime_db_recovery_pending(path)
            assert factories == [True]
    finally:
        selected.set()
        thread.join(5)
    assert not thread.is_alive()


def test_factory_close_failure_retains_physical_guard_until_creator_confirms_close(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard
    path = tmp_path / 'local.db'
    _database(path)
    failed, release = threading.Event(), threading.Event()
    captured = []
    class CloseFails(sqlite3.Connection):
        def close(self):
            if not release.is_set():
                raise sqlite3.OperationalError('physical close unconfirmed')
            return super().close()
    original = sqlite3.DatabaseError('initialization failed')
    def creator():
        def factory():
            conn = sqlite3.connect(path, factory=CloseFails)
            try:
                raise original
            except BaseException as error:
                try:
                    conn.close()
                except BaseException as cleanup:
                    error.sqlite_cleanup_error = cleanup
                    error.sqlite_unclosed_factory_connection = conn
                raise
        try:
            guarded_runtime_connect(path, factory)
        except BaseException as error:
            captured.append(error)
            failed.set()
            assert release.wait(5)
            close_runtime_connection(error.sqlite_unclosed_factory_connection)
    thread = threading.Thread(target=creator)
    thread.start()
    assert failed.wait(5)
    try:
        assert captured == [original]
        assert isinstance(original.sqlite_cleanup_error, sqlite3.OperationalError)
        with pytest.raises(sqlite3.OperationalError):
            with runtime_db_generation_guard(path, exclusive=True):
                raise AssertionError('unclosed factory conn must retain SH')
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    with runtime_db_generation_guard(path, exclusive=True):
        pass


def test_factory_close_exception_after_physical_close_does_not_leak_guard(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard
    path = tmp_path / 'local.db'
    _database(path)
    original = sqlite3.DatabaseError('initialization failed')
    def factory():
        conn = sqlite3.connect(path)
        conn.close()
        original.sqlite_unclosed_factory_connection = conn
        raise original
    with pytest.raises(sqlite3.DatabaseError) as error:
        guarded_runtime_connect(path, factory)
    assert error.value is original
    with runtime_db_generation_guard(path, exclusive=True):
        pass
