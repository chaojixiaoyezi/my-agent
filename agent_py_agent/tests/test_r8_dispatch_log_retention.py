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
        conn.commit()
    folder = tmp_path / 'background_dispatch'
    folder.mkdir()
    log = folder / 'launch.log'
    log.write_bytes(b'original recovery evidence')
    record_dispatch_log_exit(log, returncode=0, run_ids=['run'], owner_home=owner)
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
