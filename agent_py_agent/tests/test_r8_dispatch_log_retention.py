"""真实stdout SH锁、退出证明与canonical业务终态共同约束日志保留。"""
import json
import os
import sqlite3
import time
from contextlib import closing

from agent_py_agent.agent.agent_core.orchestration.background.log_retention import (
    pin_dispatch_log,
    record_dispatch_log_exit,
    retire_dispatch_logs_slice,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _case(tmp_path, *, status='done'):
    owner = tmp_path / 'owner'
    owner.mkdir()
    repo = RuntimeRepository(owner / 'runtime.db')
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute('INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)', ('t', 'task', status, 1, 1))
        conn.execute('INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)', ('a', 't', 'run', status, 1, 1))
        conn.execute('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES(?,?,?,?,?,?)', ('attempt', 'a', 1, status, 1, 2))
        conn.commit()
    folder = tmp_path / 'background_dispatch'
    folder.mkdir()
    log = folder / 'launch.log'
    log.write_bytes(b'original recovery evidence')
    record_dispatch_log_exit(log, returncode=0, run_ids=['run'], attempt_ids={'run': 'attempt'}, owner_home=owner,
        expected_identity=(log.stat().st_dev, log.stat().st_ino))
    return owner, folder, log


def test_terminal_log_expires_after_retention_and_exit_metadata_retires(tmp_path):
    owner, folder, log = _case(tmp_path)
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 1
    assert not log.exists()
    assert not log.with_suffix('.exit.json').exists()


def test_active_or_unknown_business_preserves_process_exit_evidence(tmp_path):
    owner, folder, log = _case(tmp_path, status='running')
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists() and log.with_suffix('.exit.json').exists()
    unknown = folder / 'unknown.log'
    unknown.write_bytes(b'no exit proof')
    assert retire_dispatch_logs_slice(folder, now=time.time() + 1000 * 86400)['deleted'] == 0
    assert unknown.exists()


def test_inherited_stdout_lease_protects_log_after_direct_process_exit(tmp_path):
    owner, folder, log = _case(tmp_path)
    with log.open('ab') as inherited:
        pin_dispatch_log(inherited)
        assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 0
        assert log.exists()
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 1


def test_log_modified_after_exit_is_preserved(tmp_path):
    _, folder, log = _case(tmp_path)
    marker = json.loads(log.with_suffix('.exit.json').read_text())
    os.utime(log, (marker['finished_at'] + 1, marker['finished_at'] + 1))
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists()


def test_old_unversioned_attempt_proof_and_unknown_attempt_keep_log(tmp_path):
    owner, folder, log = _case(tmp_path)
    marker = log.with_suffix('.exit.json')
    row = json.loads(marker.read_text())
    row['schema'] = 'background_dispatch_log_exit.v1'
    marker.write_text(json.dumps(row))
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    row['schema'] = 'background_dispatch_log_exit.v2'
    marker.write_text(json.dumps(row))
    with closing(sqlite3.connect(owner / 'runtime.db')) as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown'")
        conn.commit()
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists()


def test_real_dispatch_exit_proof_uses_original_attempt_and_stdout_inode(tmp_path, monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.background import dispatch, log_retention
    owner, folder, _log = _case(tmp_path)
    agent = SimpleNamespace(root=owner, subagents=SimpleNamespace(workspace=tmp_path))
    request = dispatch._BackgroundDispatchRequest(agent=agent, run_ids=['run'], launch_id='actual', router=None, cfg=None,
        params=SimpleNamespace(expected_attempt_ids={'run': 'attempt'}))
    monkeypatch.setattr(dispatch, '_background_dispatch_command', lambda *_args: [sys.executable, '-c', 'from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment\nwith bounded_stdio_from_environment(): print("actual stdout")'])
    process = dispatch._spawn_background_dispatch_process(agent, request)
    dispatch._remember_background_dispatch(agent, 'actual', ['wrong-display-run'], 'display-only')
    settled = threading.Event()
    original = log_retention.record_dispatch_log_exit
    def record(*args, **kwargs):
        original(*args, **kwargs)
        settled.set()
    monkeypatch.setattr(log_retention, 'record_dispatch_log_exit', record)
    dispatch._start_background_process_reaper(agent, request, process)
    assert settled.wait(5)
    marker = folder / 'actual.exit.json'
    row = json.loads(marker.read_text())
    assert row['run_ids'] == ['run'] and row['attempt_ids'] == {'run': 'attempt'}
    assert tuple(row['identity']) == process._my_agent_background_log_identity
    retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)
    assert not (folder / 'actual.log').exists() and not marker.exists()
    assert not (folder / 'actual.capture.json').exists()


def test_unrelated_old_replica_does_not_permanently_hold_current_launch(tmp_path):
    owner, folder, log = _case(tmp_path)
    data = owner / 'data'
    data.mkdir()
    legacy = data / 'runtime.db'
    RuntimeRepository(legacy)
    with closing(sqlite3.connect(legacy)) as conn:
        for name in ('idx_task_runs_current_task', 'idx_agent_runs_current_run', 'idx_wake_queue_current_task', 'idx_wake_queue_current_run'):
            conn.execute('DROP INDEX ' + name)
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 1
    assert not log.exists()
