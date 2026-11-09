import sqlite3
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.local_storage import LocalStore, schema
from agent_py_agent.agent.local_storage.control_plane_models import AgentRunRecord


def _seed(store, suffix=''):
    for run_id, depth in [('child-b', 1), ('parent', 0), ('child-a', 1)]:
        store.upsert_agent_run(AgentRunRecord(
            run_id=run_id + suffix, root_task_id='root', depth=depth,
            status='RUNNING', created_at=1, updated_at=2,
        ))
    store.upsert_agent_run(AgentRunRecord(run_id='foreign', root_task_id='other'))
    store.rebuild_task_rollup('root')


def test_tree_report_uses_one_short_read_connection_and_preserves_full_rows(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'local.db')
    _seed(store)
    expected_runs = store._agent_runs_for_task('root')
    expected_rollup = store.get_task_rollup('root')
    original = store._passive_connection
    connections, statements = [], []

    @contextmanager
    def observed():
        with original() as conn:
            connections.append(conn)
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(store, '_passive_connection', observed)
    report = store.list_agent_tree('root')
    assert report.runs == expected_runs
    assert report.rollup == expected_rollup
    assert [run.run_id for run in report.runs] == ['parent', 'child-a', 'child-b']
    assert len(connections) == 1
    assert len([sql for sql in statements if sql.lstrip().startswith('SELECT')]) == 2
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].execute('SELECT 1')
    assert store.list_agent_tree('missing').runs == []
    assert store.list_agent_tree('missing').rollup is None


def test_next_report_reopens_replaced_directory_without_retaining_old_database(tmp_path):
    original = LocalStore(tmp_path / 'active' / 'local.db')
    replacement = LocalStore(tmp_path / 'replacement' / 'local.db')
    _seed(original, '-old')
    _seed(replacement, '-new')
    assert all(run.run_id.endswith('-old') for run in original.list_agent_tree('root').runs)
    original.root.rename(tmp_path / 'retired')
    replacement.root.rename(original.root)
    assert all(run.run_id.endswith('-new') for run in original.list_agent_tree('root').runs)
    original.root.rename(tmp_path / 'replacement')
    (tmp_path / 'retired').rename(original.root)
    assert all(run.run_id.endswith('-old') for run in original.list_agent_tree('root').runs)


@pytest.mark.parametrize('failure', ['missing', 'directory', 'symlink', 'permission'])
def test_tree_read_preserves_source_validation_and_never_repairs(tmp_path, monkeypatch, failure):
    store = LocalStore(tmp_path / 'store' / 'local.db')
    _seed(store)
    store.db_path.rename(store.root / 'saved.db')
    expected = None
    if failure == 'directory':
        store.db_path.mkdir()
    elif failure == 'symlink':
        store.db_path.symlink_to(store.root / 'saved.db')
    elif failure == 'permission':
        (store.root / 'saved.db').rename(store.db_path)
        original = schema.require_plain_sqlite_files
        expected = PermissionError('fixture access denied')

        def denied(db, **kwargs):
            if db == store.db_path:
                raise expected
            return original(db, **kwargs)

        monkeypatch.setattr(schema, 'require_plain_sqlite_files', denied)
    with pytest.raises((FileNotFoundError, ValueError, PermissionError)) as caught:
        store.list_agent_tree('root')
    if expected is not None:
        assert caught.value is expected
    if failure == 'missing':
        assert not store.db_path.exists()


def test_second_query_failure_closes_the_same_connection_and_does_not_repair(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'local.db')
    _seed(store)
    with store._connection() as conn:
        conn.execute('DROP TABLE task_rollups')
        conn.commit()
    original = store._passive_connection
    connections = []

    @contextmanager
    def observed():
        with original() as conn:
            connections.append(conn)
            yield conn

    monkeypatch.setattr(store, '_passive_connection', observed)
    with pytest.raises(sqlite3.OperationalError, match='no such table: task_rollups'):
        store.list_agent_tree('root')
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].execute('SELECT 1')
