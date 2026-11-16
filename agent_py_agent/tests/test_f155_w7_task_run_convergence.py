"""精确收口、只读预览、代次冲突与有界分页的运行时回归。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.task_run_closeout import reconcile_task_run_closeout
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def world(tmp_path):
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    store = ConversationStore(tmp_path / 'conversations')
    run = repo.record_run_creation(owner_id='owner', goal='test', run_id='root',
                                   role='main', conversation_task_id='task')
    repo.settle_agent_attempt(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'])
    repo.settle_agent_run(agent_run_id=run['agent_run_id'], status='done')
    return SimpleNamespace(repo=repo, store=store, run=run)


def test_preview_is_readonly_and_apply_idempotent(world):
    repo, store, run = world.repo, world.store, world.run
    before = repo.db_path.read_bytes()
    result = reconcile_task_run_closeout(repo, store, task_run_id=run['task_run_id'])
    assert result['decision'] == 'eligible'
    assert repo.db_path.read_bytes() == before
    assert reconcile_task_run_closeout(repo, store, task_run_id=run['task_run_id'], apply=True)['applied']
    assert reconcile_task_run_closeout(repo, store, task_run_id=run['task_run_id'], apply=True)['decision'] == 'already_terminal'


def test_unreadable_link_never_means_absence(world, monkeypatch):
    def corrupt(_task_id):
        raise ValueError('corrupt')
    monkeypatch.setattr(world.store.tasks, 'load', corrupt)
    result = reconcile_task_run_closeout(world.repo, world.store, task_run_id=world.run['task_run_id'], apply=True)
    assert result['reason_code'] == 'proof_read_failed'
    assert world.repo.get_task_run(world.run['task_run_id'])['closed_at'] == 0


def test_unknown_terminal_child_is_protected(world):
    child = world.repo.create_agent_run(task_run_id=world.run['task_run_id'],
                                        parent_agent_run_id=world.run['agent_run_id'], run_id='child')
    attempt = world.repo.create_attempt(child['agent_run_id'])
    with world.repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown',ended_at=1 WHERE attempt_id=?", (attempt['attempt_id'],))
        conn.execute("UPDATE agent_runs SET status='cancelled' WHERE agent_run_id=?", (child['agent_run_id'],))
    result = reconcile_task_run_closeout(world.repo, world.store, task_run_id=world.run['task_run_id'], apply=True)
    assert result['reason_code'] == 'attempt_unknown'
    assert world.repo.get_task_run(world.run['task_run_id'])['closed_at'] == 0


def test_new_attempt_in_commit_callback_blocks_close(world):
    def transition(operation):
        world.repo.create_attempt(world.run['agent_run_id'])
        return operation()
    result = reconcile_task_run_closeout(world.repo, world.store, task_run_id=world.run['task_run_id'],
                                         apply=True, commit_transition=transition)
    assert not result['applied']
    assert result['reason_code'] == 'agent_tree_active'


def test_open_page_keyset_and_index(world):
    repo = world.repo
    for _ in range(8):
        repo.create_task_run(task_id=world.run['task_id'])
    ids, cursor = [], {}
    while True:
        page = repo.open_task_runs_page(limit=2, **cursor)
        assert len(page['items']) <= 2
        ids.extend(row['task_run_id'] for row in page['items'])
        if not page['partial']:
            break
        cursor = page['next_cursor']
    assert len(ids) == len(set(ids)) == 9
    with repo._runtime_connection() as conn:
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM task_runs WHERE closed_at=0 AND '
                            '(created_at,task_run_id) > (?,?) ORDER BY created_at,task_run_id LIMIT 3', (0, '')).fetchall()
    assert any('idx_task_runs_open_page' in row['detail'] for row in plan)


def test_conflict_dedupe_exact_generation(world):
    args = dict(agent_run_id=world.run['agent_run_id'], attempt_id=world.run['attempt_id'],
                source_generation='1:link', reason='locator_unresolved', task_id=world.run['task_id'])
    for _ in range(30):
        world.repo.append_conflict_once(**args)
    args['source_generation'] = '2:link'
    world.repo.append_conflict_once(**args)
    with world.repo._runtime_connection() as conn:
        rows = conn.execute("SELECT * FROM runtime_events WHERE event_type='status_conflict'").fetchall()
    assert len(rows) == 2


@pytest.mark.parametrize('phase', ['publish', 'workspace'])
@pytest.mark.parametrize('link_state', ['absent', 'active', 'corrupt'])
def test_premodel_failure_preserves_exception_and_closes_wrapper(tmp_path, monkeypatch, phase, link_state):
    from agent_py_agent.agent.agent_core import runtime_mixin as runtime
    from agent_py_agent.agent.agent_core.runtime.loop_support import RunParams

    repo = RuntimeRepository(tmp_path / 'runtime.db')
    store = ConversationStore(tmp_path / 'conversations')
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), conversation_store=store,
                            home_paths=SimpleNamespace(owner_id='local/main'))
    if link_state == 'active':
        import json
        path = store.storage.task_path('premodel-task')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken' if link_state == 'corrupt' else json.dumps({
            'task_id': 'premodel-task', 'thread_id': 'thread', 'status': 'active'}))
    failure = ValueError('injected premodel failure')
    def fail(*_args, **_kwargs):
        if link_state == 'corrupt':
            path = store.storage.task_path('premodel-task')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{broken')
        raise failure
    binding = SimpleNamespace(bind_runtime_authority=fail) if phase == 'publish' else None
    params = RunParams(run_id='premodel', task_id='premodel-task', conversation_task_binding_callback=binding)
    if phase == 'workspace':
        monkeypatch.setattr(runtime, 'attach_run_task_workspace_context', fail)
        monkeypatch.setattr(runtime, 'is_interrupted', lambda: False)
        def invoke():
            return runtime._bind_main_agent_turn_params(agent, 'test', params)
    else:
        def invoke():
            return runtime._bind_main_agent_authority(agent, params)
    with pytest.raises(ValueError) as caught:
        invoke()
    assert caught.value is failure
    row = repo.agent_run_for_run_id('premodel')
    assert row['status'] == 'failed'
    assert (repo.get_task_run(row['task_run_id'])['closed_at'] > 0) is (link_state == 'absent')


@pytest.mark.parametrize('complete', [False, True])
def test_empty_wrapper_requires_certified_absence(world, complete):
    from contextlib import contextmanager

    run = world.repo.create_task_run(task_id=world.run['task_id'])
    @contextmanager
    def proof(row, *, apply):
        yield {'schema': 'task-run-empty-reference-proof.v1', 'task_run_id': row['task_run_id'],
               'task_id': row['task_id'], 'generation': {key: row[key] for key in ('created_at', 'updated_at')},
               'coverage_complete': complete, 'references_absent': True, 'execution_absent': True}
    preview = reconcile_task_run_closeout(world.repo, world.store, task_run_id=run['task_run_id'],
                                          empty_reference_guard=proof)
    assert (preview['decision'] == 'eligible') is complete
    result = reconcile_task_run_closeout(world.repo, world.store, task_run_id=run['task_run_id'],
                                         empty_reference_guard=proof, apply=True)
    assert result['applied'] is complete
    assert (world.repo.get_task_run(run['task_run_id'])['closed_at'] > 0) is complete


def test_cold_readonly_repository_creates_no_application_lock_or_schema(world, tmp_path):
    import sqlite3

    directory = tmp_path / 'cold'
    directory.mkdir(mode=0o700)
    path = directory / 'runtime.db'
    with world.repo._runtime_connection() as source:
        destination = sqlite3.connect(path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    before = path.read_bytes()
    reader = RuntimeRepository(path, read_only=True)
    assert reader.get_task_run(world.run['task_run_id']) is not None
    assert reader.open_task_runs_page(limit=1)['items']
    assert path.read_bytes() == before
    assert not path.with_name('runtime.db.generation.lock').exists()
