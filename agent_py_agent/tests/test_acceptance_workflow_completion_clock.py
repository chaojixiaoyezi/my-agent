"""用真实运行及 Event 交错验证完成计时不覆盖并发正文、范围与运行终态。"""
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.workflows.business_scope import workflow_state
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.reference_sources import WorkflowManagementService


def definition(name='completion-clock', steps=None):
    return {'descriptor': {'id': name, 'kind': 'workflow', 'name': name,
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': steps or []}


def service(home, now, handler=None):
    actor = SimpleNamespace(owner_id='completion-owner')
    api = WorkflowManagementService(home, owner_id=actor.owner_id,
        authorize=lambda current, action, ref: current is actor, clock=lambda: now[0],
        engine_factory=lambda root, **options: WorkflowEngine(root, analysis_handler=handler, **options))
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'owner_global', 'space_ids': []}}, actor=actor, idempotency_key='create')
    return api, actor


def mutate(api, actor, action, params, key):
    current = api._get('completion-clock')
    revision = (workflow_state(current)['scope']['scope_revision'] if action == 'workflows.scope.set'
                else current.descriptor.revision)
    return api.execute(action, {'workflow_ref': 'completion-clock', **params}, actor=actor,
                       expected_revision=revision, idempotency_key=key)


def test_completion_then_concurrent_edit_preserves_newer_anchor_and_scope(tmp_path, monkeypatch):
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    published, release = Event(), Event()
    original = WorkflowStore._record_completed_run

    def paused(store, record, snapshot):
        published.set()
        assert release.wait(10), '未释放完成发表交错'
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(api.execute, 'workflows.run', {'workflow_ref': 'completion-clock'},
                             actor=actor, idempotency_key='run')
        assert published.wait(10)
        try:
            # 原 run.json 已终态，定义尚未计时；编辑必须不等待运行执行锁。
            now[0] = time.time() + 60
            pool.submit(mutate, api, actor, 'workflows.scope.set',
                {'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, 'unassign').result(timeout=5)
            changed = definition()
            changed['body_markdown'] = '完成后并发编辑'
            pool.submit(mutate, api, actor, 'workflows.update', {'definition': changed}, 'edit').result(timeout=5)
        finally:
            release.set()
        receipt = future.result(timeout=10)
    run = api._run_store().load_run(receipt['workflow_run_id'])
    saved = api._get('completion-clock')
    state = workflow_state(saved)
    assert run.status == 'succeeded'
    assert state['last_run_at'] == run.ended_at < now[0]
    assert state['orphan_since'] == state['last_content_edit_at'] == now[0]
    assert state['scope']['scope_mode'] == 'unassigned'
    assert saved.body_markdown == changed['body_markdown']
    assert saved.descriptor.owner_id == actor.owner_id
    # 同终态重放、读取、摘要合并都不能把处理时刻变成新的活动。
    before = copy.deepcopy(state)
    api._run_store().save_run(run)
    api.execute('workflows.get', {'workflow_ref': 'completion-clock'}, actor=actor)
    api.store.fold_run_summaries('completion-clock')
    assert workflow_state(api._get('completion-clock')) == before
    stale = replace(run, status='running')
    with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
        api._run_store().save_run(stale)
    assert api._run_store().load_run(run.workflow_run_id).status == 'succeeded'


def test_run_unassigns_then_completion_refreshes_and_reads_do_not(tmp_path):
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    unassigned = []

    def analyze(spec, context, skills):
        mutate(api, actor, 'workflows.scope.set',
            {'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, 'unassign')
        unassigned.append(workflow_state(api._get('completion-clock')))
        return {'done': True}

    api.engine_factory = lambda root, **options: WorkflowEngine(root, analysis_handler=analyze, **options)
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'work', 'name': 'work', 'mode': 'llm_skill_analysis'}])}, 'steps')
    receipt = api.execute('workflows.run', {'workflow_ref': 'completion-clock'}, actor=actor, idempotency_key='run')
    run = api._run_store().load_run(receipt['workflow_run_id'])
    state = workflow_state(api._get('completion-clock'))
    assert state['orphan_since'] == state['last_run_at'] == run.ended_at
    assert run.ended_at > unassigned[0]['orphan_since']
    now[0] += 10 * 86400
    for action in ('workflows.list', 'workflows.get', 'workflows.preview'):
        params = {} if action == 'workflows.list' else {'workflow_ref': 'completion-clock'}
        api.execute(action, params, actor=actor)
    api.store.fold_run_summaries('completion-clock')
    assert workflow_state(api._get('completion-clock')) == state


@pytest.mark.parametrize('field,value', [('scope_layer', 'workspace'), ('source_scope', 'builtin'),
                                        ('owner_id', 'another-owner')])
def test_completion_from_other_source_does_not_touch_owner_clock(tmp_path, field, value):
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    receipt = api.execute('workflows.run', {'workflow_ref': 'completion-clock'}, actor=actor, idempotency_key='real-run')
    completed = api._run_store().load_run(receipt['workflow_run_id'])
    source = api._get('completion-clock').to_dict()
    source['descriptor'][field] = value
    before = workflow_state(api._get('completion-clock'))
    # 存储合同的负例：另一来源的完成记录不能污染本 Owner 同名资产。
    result = replace(completed, workflow_run_id='other-source', ended_at=completed.ended_at + 60,
                     owner_id=source['descriptor']['owner_id'], revision=0)
    api._run_store().create_run(result, source)
    assert api._run_store().load_run(result.workflow_run_id).status == 'succeeded'
    assert workflow_state(api._get('completion-clock')) == before


def test_summary_observation_and_unsettled_terminal_do_not_extend_clock(tmp_path):
    from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    snapshot = api._get('completion-clock').to_dict()
    before = workflow_state(api._get('completion-clock'))
    api.store.record_run_summary('completion-clock', 'succeeded', 1, blocking=True, run_id='summary-only')
    assert workflow_state(api._get('completion-clock')) == before
    step = StepExecutionRecord(step_id='delegated', name='等待原任务', mode='agent_task',
        status='waiting_for_agent', started_at=now[0], ended_at=0, duration_ms=0, llm_involved=False,
        escalation_result={'task_id': 'pending-task', 'effect_started': True})
    record = WorkflowRunRecord(workflow_run_id='unsettled', workflow_id='completion-clock',
        owner_id=actor.owner_id, trigger_kind='manual', status='cancelled',
        started_at=now[0], ended_at=time.time(), duration_ms=1, steps=(step,))
    api._run_store().create_run(record, snapshot)
    assert workflow_state(api._get('completion-clock')) == before
    # 记录取消证明后才允许推进；晚到旧完成不擦除这份原步骤证明。
    proof = {'task_id': 'pending-task', 'effect_started': True,
             'cancellation_receipt': {'state': 'completed', 'outstanding_refs': []}}
    record.steps = (replace(step, status='failed', escalation_result=proof),)
    api._run_store().save_run(record)
    assert workflow_state(api._get('completion-clock'))['last_run_at'] == record.ended_at
    assert api._run_store().load_run(record.workflow_run_id).steps[0].escalation_result == proof


def test_unassigned_completed_run_moves_cleanup_due_index(tmp_path):
    now = [time.time() - 40 * 86400]
    api, actor = service(tmp_path, now)
    mutate(api, actor, 'workflows.scope.set', {'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, 'unassign')
    # 只有运行开始前具备范围才可执行；此处从真实冻结启动后在步骤中撤销范围。
    mutate(api, actor, 'workflows.scope.set', {'scope': {'scope_mode': 'owner_global', 'space_ids': []}}, 'assign')

    def analyze(*args):
        mutate(api, actor, 'workflows.scope.set', {'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, 'unassign-again')
        return {'ok': True}

    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'work', 'name': 'work', 'mode': 'llm_skill_analysis'}])}, 'steps')
    api.engine_factory = lambda root, **options: WorkflowEngine(root, analysis_handler=analyze, **options)
    api.execute('workflows.run', {'workflow_ref': 'completion-clock'}, actor=actor, idempotency_key='run')
    now[0] = time.time()
    result = api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='cleanup')
    assert result['counts']['recycled'] == 0
    assert workflow_state(api._get('completion-clock'))['lifecycle'] == 'active'


def test_same_key_retry_repairs_clock_after_terminal_publish_without_execution(tmp_path, monkeypatch):
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    calls = []

    def analyze(*args):
        calls.append('execute')
        return {'done': True}

    api.engine_factory = lambda root, **options: WorkflowEngine(root, analysis_handler=analyze, **options)
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'work', 'name': 'work', 'mode': 'llm_skill_analysis'}])}, 'steps')
    original = WorkflowStore._record_completed_run

    def unavailable(store, record, snapshot):
        if record.status == 'succeeded':
            raise OSError('完成计时暂不可写')
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', unavailable)
    from agent_py_agent.agent.workflows.run_store import WorkflowExecutionError
    with pytest.raises(WorkflowExecutionError, match='完成计时暂不可写') as raised:
        api.execute('workflows.run', {'workflow_ref': 'completion-clock'}, actor=actor, idempotency_key='run')
    assert isinstance(raised.value.cause, OSError)
    assert raised.value.receipt['status'] == 'succeeded'
    assert raised.value.receipt['phase'] == 'completion_clock'
    assert workflow_state(api._get('completion-clock'))['last_run_at'] is None
    monkeypatch.setattr(WorkflowStore, '_record_completed_run', original)
    receipt = api.execute('workflows.run', {'workflow_ref': 'completion-clock'}, actor=actor, idempotency_key='run')
    run = api._run_store().load_run(receipt['workflow_run_id'])
    assert run.status == 'succeeded' and calls == ['execute']
    assert workflow_state(api._get('completion-clock'))['last_run_at'] == run.ended_at


def test_completion_releases_execution_lock_before_waiting_for_asset_lock(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import asset_root_lock
    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF, _HELD_LOCKS
    now = [time.time() - 60]
    entered, release, publishing = Event(), Event(), Event()

    def analyze(*args):
        entered.set()
        assert release.wait(10)
        return {'done': True}

    api, actor = service(tmp_path, now, analyze)
    mutate(api, actor, 'workflows.update', {'definition': definition(steps=[
        {'step_id': 'work', 'name': 'work', 'mode': 'llm_skill_analysis'}])}, 'steps')
    original = WorkflowStore._record_completed_run
    observed = []

    def observe(store, record, snapshot):
        observed.append((record.workflow_run_id, not getattr(_HELD_LOCKS, 'paths', set()),
                         _COMPLETION_HANDOFF.get() is None))
        publishing.set()
        return original(store, record, snapshot)

    monkeypatch.setattr(WorkflowStore, '_record_completed_run', observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(api.execute, 'workflows.run', {'workflow_ref': 'completion-clock'},
                             actor=actor, idempotency_key='run')
        assert entered.wait(10)
        try:
            with asset_root_lock(api.store.owner_home):
                release.set()
                assert publishing.wait(10)
                # 完成线程正等待主线程的资产锁；反向读运行必须仍能完成。
                run = pool.submit(api._run_store().load_run, observed[0][0]).result(timeout=5)
                assert run.status == 'succeeded'
        finally:
            release.set()
        future.result(timeout=10)
    assert observed == [(run.workflow_run_id, True, True)]


def test_nested_run_locks_defer_clock_until_parent_exit(tmp_path):
    from agent_py_agent.agent.workflows.run_store import _COMPLETION_HANDOFF, _HELD_LOCKS
    now = [time.time() - 60]
    api, actor = service(tmp_path, now)
    with api._run_store().execution_lock('parent-lock'):
        receipt = api.execute('workflows.run', {'workflow_ref': 'completion-clock'},
                              actor=actor, idempotency_key='nested-run')
        assert receipt['state'] == 'completed'
        assert workflow_state(api._get('completion-clock'))['last_run_at'] is None
    run = api._run_store().load_run(receipt['workflow_run_id'])
    assert workflow_state(api._get('completion-clock'))['last_run_at'] == run.ended_at
    assert not _HELD_LOCKS.paths and _COMPLETION_HANDOFF.get() is None
