"""对账的真实权威读回、无写预览及原 reducer/CAS 行为。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.runtime_reconciliation import (
    _snapshot,
    inspect_parent_terminal,
    reconcile_capability_page,
    reconcile_owner_runtime,
)
from agent_py_agent.agent.tooling.runtime_governance_tool import RuntimeGovernanceTool
from agent_py_agent.tests.test_fw41_w2_capability_and_idle_approvals import _make_blocked_run
from agent_py_agent.tests.test_schedule_control import _agent


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def _terminal_root(agent, root='scheduled-root'):
    repo = agent.subagents.runtime_db
    created = repo.record_run_creation(owner_id=agent.home_paths.owner_id,
        conversation_task_id=root, run_id=root, role='scheduled', attempt_status='pending')
    attempt = repo.create_attempt(created['agent_run_id'], reuse_pending=True)
    repo.settle_agent_run(agent_run_id=created['agent_run_id'], status='done', attempt_id=attempt['attempt_id'])
    return created


def test_live_wal_preview_reads_committed_wal_and_never_immutable(tmp_path):
    path = tmp_path / 'proof.sqlite3'
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE facts(value TEXT)')
        writer.commit()
        writer.execute("INSERT INTO facts VALUES('committed WAL authority')")
        writer.commit()
        with _snapshot(path) as reader:
            assert reader.execute('SELECT value FROM facts').fetchone()[0] == 'committed WAL authority'


def test_cold_wal_native_reader_does_not_initialize_application_state(tmp_path):
    path = tmp_path / 'proof.sqlite3'
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE facts(value TEXT)')
        writer.commit()
    before = _files(tmp_path)
    with _snapshot(path) as reader:
        assert reader.execute('SELECT * FROM facts').fetchall() == []
    after = _files(tmp_path)
    assert after[path.name] == before[path.name]
    assert set(after) - set(before) <= {path.name + '-wal', path.name + '-shm'}


def test_scheduled_root_exact_runtime_proof_and_capability_preview_apply(tmp_path):
    agent = _agent(tmp_path)
    root = 'srun-exact-owner'
    _terminal_root(agent, root)
    child, request_id = _make_blocked_run(agent, parent_id=root, root_id=root, depth=1)
    with closing(sqlite3.connect(agent.subagents.runtime_db.db_path)) as live:
        live.execute('SELECT * FROM tasks').fetchall()
        proof = inspect_parent_terminal(agent, root)
        assert proof['eligible'], proof
        before = _files(agent.home_paths.owner_home_dir)
        page = reconcile_capability_page(agent, run_ids=(child.id,), limit=1)
        assert page['items'][0]['eligible'], page
        assert not page['items'][0]['repaired']
        assert _files(agent.home_paths.owner_home_dir) == before
        outcome = reconcile_capability_page(agent, run_ids=(child.id,), limit=1, apply=True)
        assert outcome['items'][0]['repaired'], outcome
        current = agent.subagents.load(child.id)
        assert current.capability_requests[0].id == request_id
        assert current.capability_requests[0].status == 'CLOSED'
        assert current.status == 'CANCELLED'
        replay = reconcile_capability_page(agent, run_ids=(child.id,), limit=1, apply=True)
        assert not replay['items'][0]['repaired']


def test_created_or_unknown_attempt_never_authorizes_parent_closure(tmp_path):
    agent = _agent(tmp_path)
    root = 'srun-never-assume-unstarted'
    repo = agent.subagents.runtime_db
    created = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id=root,
        run_id=root, role='scheduled', attempt_status='pending')
    with closing(sqlite3.connect(repo.db_path)) as live:
        live.execute('SELECT * FROM tasks').fetchall()
        assert inspect_parent_terminal(agent, root)['reason_code'] == 'ROOT_ACTIVE'
        live.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (created['agent_run_id'],))
        live.commit()
        assert inspect_parent_terminal(agent, root)['reason_code'] == 'ATTEMPT_UNKNOWN'


def test_foreign_owner_root_is_protected_even_when_terminal(tmp_path):
    agent = _agent(tmp_path)
    created = _terminal_root(agent)
    with closing(sqlite3.connect(agent.subagents.runtime_db.db_path)) as live:
        live.execute("UPDATE tasks SET owner_id='different-owner' WHERE task_id=?", (created['task_id'],))
        live.commit()
        assert inspect_parent_terminal(agent, 'scheduled-root')['reason_code'] == 'OWNER_MISMATCH'


def test_public_actions_have_distinct_effects_and_cannot_model_forge_foreground(tmp_path):
    agent = _agent(tmp_path)
    tool = RuntimeGovernanceTool(agent, is_admin=True)
    choices = tool.model_spec.input_schema['properties']['action']['enum']
    assert 'reconcile_preview' in choices and 'reconcile_apply' in choices
    policy = dict(dict(tool.runtime_policy.effect_resolver.by_parameter)['action'])
    assert policy['reconcile_preview'] == 'read_only'
    assert policy['reconcile_apply'] == 'dangerous'
    agent._current_run_params = SimpleNamespace()
    denied = tool.execute({'action': 'reconcile_apply', 'admin_authorized': True})
    assert not denied.ok and denied.effect_outcome == 'not_started'
    invalid = tool.execute({'action': 'reconcile_preview', 'host_foreground_interactive': True})
    assert not invalid.ok


def test_cursor_bound_to_owner_filter_and_page_budget(tmp_path):
    agent = _agent(tmp_path)
    result = reconcile_owner_runtime(agent, run_ids=('missing-child',), limit=1)
    token = json.loads(result['next_cursor'])
    token['owner'] = 'different-owner'
    with pytest.raises(ValueError, match='cursor owner'):
        reconcile_owner_runtime(agent, run_ids=('missing-child',), limit=1, cursor=json.dumps(token))
    with pytest.raises(ValueError, match='run_ids'):
        reconcile_owner_runtime(agent, run_ids=('first', 'second'), limit=1)
    with pytest.raises(ValueError, match='run_ids'):
        reconcile_owner_runtime(agent, run_ids=('../escape',), limit=1)


def test_explicit_authority_missing_archive_keeps_status_honest_and_unique_artifacts(tmp_path):
    import os
    import time

    from agent_py_agent.agent.subagents.services.persistence.locator_hygiene import (
        reconcile_projection_page,
    )
    from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget

    agent = _agent(tmp_path)
    _terminal_root(agent, 'terminal-parent')
    home = agent.home_paths.owner_home_dir
    run = home / 'agents' / 'legacy-phantom'
    run.mkdir(parents=True)
    old = time.time() - 40 * 86400
    payload = {'run_id': run.name, 'owner': agent.home_paths.owner_id, 'root_id': 'terminal-parent',
        'parent_id': 'terminal-parent', 'status': 'RUNNING', 'updated_at': old}
    (run / 'state.json').write_text(json.dumps(payload))
    os.utime(run / 'state.json', (old, old))
    with closing(sqlite3.connect(agent.subagents.runtime_db.db_path)) as live:
        live.execute('SELECT * FROM tasks').fetchall()
        before = _files(home)
        budget = StorageLaneBudget(1000, 1024 * 1024, None)
        preview = reconcile_projection_page(agent, run_ids=(run.name,), budget=budget)
        assert preview['items'][0]['eligible'], preview
        assert _files(home) == before
        # 唯一资产是硬保护；删除资产后再新预览方可归档。
        (run / 'important.bin').write_bytes(b'unique accepted bytes')
        protected = reconcile_projection_page(agent, run_ids=(run.name,), apply=True, budget=budget)
        assert not protected['items'][0]['eligible']
        assert (run / 'important.bin').read_bytes() == b'unique accepted bytes'
        (run / 'important.bin').unlink()
        outcome = reconcile_projection_page(agent, run_ids=(run.name,), apply=True, budget=budget)
        assert outcome['archived_dirs'] == 1, outcome
        assert not run.exists()
        archives = list((home / 'agents' / 'archived_subagents').glob('*/*.jsonl'))
        receipt = json.loads(archives[0].read_text().splitlines()[0])
        assert receipt['authority_state'] == 'authority_missing'
        assert receipt['status'] == 'RUNNING'


def test_coordinator_reuses_real_w7_exact_closeout_and_w8_index_page(tmp_path):
    agent = _agent(tmp_path)
    created = _terminal_root(agent)
    repo = agent.subagents.runtime_db
    preview = reconcile_owner_runtime(agent, limit=10)
    selected = [row for row in preview['items'] if row.get('task_run_id') == created['task_run_id']]
    assert selected and selected[0]['decision'] == 'eligible', preview
    assert repo.get_task_run(created['task_run_id'])['closed_at'] == 0
    applied = reconcile_owner_runtime(agent, apply=True, limit=10)
    assert applied['repaired'] >= 1, applied
    assert repo.get_task_run(created['task_run_id'])['closed_at'] > 0
    token = json.loads(preview['next_cursor'])
    token['phase'], token['after'] = 'indexes', ''
    indexed = reconcile_owner_runtime(agent, cursor=json.dumps(token), limit=10)
    assert indexed['items'][0]['kind'] == 'indexes', indexed
    assert 'owner_id' in indexed['items'][0]


def test_capability_byte_budget_rejects_before_reading_large_state(tmp_path):
    from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget

    agent = _agent(tmp_path)
    child, _ = _make_blocked_run(agent, parent_id='root', root_id='root', depth=1)
    before = _files(agent.home_paths.owner_home_dir)
    page = reconcile_capability_page(agent, run_ids=(child.id,), budget=StorageLaneBudget(10, 1, None))
    assert page['items'][0]['reason_code'] == 'CAPABILITY_PAGE_BYTE_BUDGET_EXHAUSTED'
    assert not page['items'][0]['repaired']
    assert _files(agent.home_paths.owner_home_dir) == before
