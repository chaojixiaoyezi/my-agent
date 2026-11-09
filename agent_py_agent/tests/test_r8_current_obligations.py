"""真实SQLite VM预算/EXPLAIN证明终态历史不会扩大当前义务查询。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from agent_py_agent.agent.runtime_db.current_obligations import (
    closed_attempt_bindings,
    closed_task_ids,
    recorded_run_ids,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.mark.parametrize('history', [10, 10_000])
def test_terminal_inventory_proofs_seek_current_indexes_inside_fixed_vm_budget(tmp_path, history):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.executemany('INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)',
            ((f't-{i}', 'target', 'done', 1, 1) for i in range(history)))
        conn.executemany('INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            ((f'a-{i}', f't-{i}', 'same-run', 'done', 1, 1) for i in range(history)))
        conn.executemany('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES(?,?,?,?,?,?)',
            ((f'at-{i}', f'a-{i}', 1, 'done', 1, 2) for i in range(history)))
        conn.executemany('INSERT INTO wake_queue(wake_id,root_task_id,root_run_id,kind,next_due_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            ((f'w-{i}', 'target', 'same-run', 'test', 1, 'consumed', 1, 1) for i in range(history)))
        conn.commit()
        queries, steps = [], []
        conn.set_trace_callback(queries.append)
        def progress():
            steps.append(True)
            return len(steps) >= 20
        conn.set_progress_handler(progress, 100)
        try:
            assert recorded_run_ids(conn, ['same-run', 'missing']) == {'same-run'}
            assert closed_task_ids(conn, ['target', 'missing']) == {'target'}
            assert closed_attempt_bindings(conn, {'same-run': f'at-{history-1}'}) == (True, {'target'})
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_trace_callback(None)
        assert len(steps) < 20  # 10与10000条终态库存共用<2000条VM指令的同一上限。
        plans = [detail for query in queries for *_unused, detail in conn.execute('EXPLAIN QUERY PLAN ' + query)]
        for index in ('idx_task_runs_current_task', 'idx_agent_runs_current_run', 'idx_wake_queue_current_task', 'idx_wake_queue_current_run'):
            assert any(index in plan for plan in plans), plans
        assert not any('SCAN task_runs' in plan or 'SCAN t ' in plan for plan in plans), plans


def test_unknown_and_current_obligations_are_never_empty_terminal_proofs(tmp_path):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES('t','target','done',1,1)")
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES('a','t','run','unknown',1,1)")
        conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES('at','a',1,'done',1,2)")
        assert closed_task_ids(conn, ['target']) == set()
        assert closed_attempt_bindings(conn, {'run': 'at'})[0] is False
        conn.execute("UPDATE agent_runs SET status='done'")
        conn.execute("INSERT INTO wake_queue(wake_id,root_task_id,root_run_id,kind,next_due_at,status,created_at,updated_at) VALUES('w','target','run','test',1,'pending',1,1)")
        assert closed_task_ids(conn, ['target']) == set()
        assert closed_attempt_bindings(conn, {'run': 'at'})[0] is False
        conn.execute("UPDATE wake_queue SET status='consumed'")
        assert closed_task_ids(conn, ['target']) == {'target'}
        assert closed_attempt_bindings(conn, {'run': 'at'}) == (True, {'target'})
        conn.execute('DROP INDEX idx_wake_queue_current_task')
        with pytest.raises(sqlite3.OperationalError):
            closed_task_ids(conn, ['target'])


def test_old_schema_is_unknown_until_normal_canonical_schema_migration(tmp_path):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES('t','target','done',1,1)")
        conn.execute('DROP INDEX idx_task_runs_current_task')
        conn.execute("UPDATE metadata SET value='pre-r8-schema' WHERE key='sqlite_schema_fingerprint'")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            closed_task_ids(conn, ['target'])
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        assert closed_task_ids(conn, ['target']) == {'target'}
        assert conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0] != 'pre-r8-schema'
