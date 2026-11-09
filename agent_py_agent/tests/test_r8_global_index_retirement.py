"""唯一历史key按canonical消失证明退役，未知及可定位历史保持。"""
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space.home_index_compact import compact_index_file
from agent_py_agent.agent.user_space.home_layout import home_paths


def _case(tmp_path):
    home = home_paths(tmp_path / 'home')
    home.owner_home_dir.mkdir(parents=True)
    repo = RuntimeRepository(home.owner_home_dir / 'runtime.db')
    with closing(sqlite3.connect(repo.db_path)) as conn:
        for task, status in [('gone', 'done'), ('exists', 'done'), ('active', 'running'), ('unknown', 'done')]:
            conn.execute('INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)',
                (task, task, status, 1, 1))
        conn.commit()
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in ('gone', 'exists', 'active', 'unknown'):
        canonical = home.owner_home_dir / 'tasks' / task
        if task == 'exists':
            canonical.mkdir(parents=True)
        rows.append({'schema_version': 'global-task-index.v1', 'owner_id': 'local/main', 'task_id': task,
            'task_path': str(canonical), 'status': 'UNKNOWN' if task == 'unknown' else 'done',
            'updated_at': datetime.fromtimestamp(time.time() - 100 * 86400, timezone.utc).isoformat()})
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return home, path, rows


def test_retire_terminal_missing_only_keep_live_unknown_and_historical_locator(tmp_path):
    _, path, _ = _case(tmp_path)
    result = compact_index_file(path, retire_stale_keys=True)
    assert result.compacted
    assert [json.loads(line)['task_id'] for line in path.read_text().splitlines()] == ['exists', 'active', 'unknown']


def test_canonical_restored_before_commit_aborts_stale_retirement(tmp_path):
    _, path, rows = _case(tmp_path)
    before = path.read_bytes()
    def commit(operation):
        from pathlib import Path
        Path(rows[0]['task_path']).mkdir(parents=True)
        return operation()
    result = compact_index_file(path, retire_stale_keys=True, commit_transition=commit)
    assert not result.compacted and result.reason == 'canonical_changed'
    assert path.read_bytes() == before


def test_index_tail_is_preserved_during_retirement(tmp_path):
    _, path, _ = _case(tmp_path)
    tail = {'owner_id': 'local/main', 'task_id': 'new', 'status': 'running'}
    def commit(operation):
        with path.open('a') as stream:
            stream.write(json.dumps(tail) + '\n')
        return operation()
    result = compact_index_file(path, retire_stale_keys=True, commit_transition=commit)
    assert result.compacted
    assert json.loads(path.read_text().splitlines()[-1]) == tail


def test_missing_owner_terminal_refs_retire_but_unknown_kept(tmp_path):
    home, path, rows = _case(tmp_path)
    for row in rows:
        row['owner_id'] = 'local/deleted'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    assert compact_index_file(path, retire_stale_keys=True).compacted
    assert {json.loads(line)['task_id'] for line in path.read_text().splitlines()} == {'exists', 'unknown'}
    assert home.owner_home_dir.exists()
