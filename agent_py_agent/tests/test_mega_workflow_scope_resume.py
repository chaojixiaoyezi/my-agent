"""范围/生命周期/异步恢复的组合合同；宿主依赖以精确端口 fake 注入。"""
import pytest

from agent_py_agent.tests.test_business_v12_f4_workflows import definition, service


def test_orphan_recycle_restore_and_reads_do_not_extend_deadline(tmp_path):
    now = [100.0]
    api, actor, _ = service(tmp_path, clock=lambda: now[0],
        references=lambda ref: {'coverage': 'complete', 'blockers': []})
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='create')
    first = api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    now[0] += 31 * 86400
    api.execute('workflows.list', {}, actor=actor)
    current = api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    assert first['cleanup_status']['orphan_since'] == current['cleanup_status']['orphan_since']
    result = api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='tick')
    assert result['counts'] == {'recycled': 1, 'skipped': 0, 'failed': 0, 'unknown': 0}
    assert api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='tick') == result
    current = api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    assert current['workflow']['lifecycle'] == 'recycled'
    restored = api.execute('workflows.restore', {'workflow_ref': 'daily'}, actor=actor,
        expected_revision=current['workflow']['definition_revision'], idempotency_key='restore')
    assert restored['scope'] == {'scope_mode': 'unassigned', 'space_ids': []}
    assert restored['cleanup_status']['orphan_since'] != first['cleanup_status']['orphan_since']
    assert api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='new-tick')['counts']['recycled'] == 0


@pytest.mark.parametrize('kind', ['active_run', 'paused_run', 'subscription', 'workflow_dependency', 'governance', 'unknown'])
def test_orphan_references_and_unknown_coverage_protect(tmp_path, kind):
    now = [100.0]
    reference = {'coverage': 'unknown', 'blockers': []} if kind == 'unknown' else {
        'coverage': 'complete', 'blockers': [{'ref': 'reference-1', 'kind': kind, 'reason_code': 'IN_USE'}]}
    api, actor, _ = service(tmp_path, clock=lambda: now[0], references=lambda ref: reference)
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='c')
    now[0] += 31 * 86400
    result = api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='tick')
    assert result['counts']['recycled'] == 0
    assert api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)['workflow']['lifecycle'] == 'active'


def test_cleanup_slice_is_bounded_and_continues_after_deletion(tmp_path):
    now = [100.0]
    api, actor, _ = service(tmp_path, clock=lambda: now[0], references=lambda ref: {'coverage': 'complete', 'blockers': []})
    for index in range(130):
        api.execute('workflows.create', {'definition': definition(f'w{index:03}'),
            'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key=f'c{index}')
    now[0] += 31 * 86400
    first = api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='first')
    second = api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='second')
    assert first['counts']['recycled'] == 128 and first['next_cursor'] is not None
    assert second['counts']['recycled'] == 2


def test_policy_protection_and_real_edit_refresh_orphan_start(tmp_path):
    now = [100.0]
    api, actor, _ = service(tmp_path, clock=lambda: now[0], references=lambda ref: {'coverage': 'complete', 'blockers': []})
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='c')
    row = api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    api.execute('workflows.protection.set', {'workflow_ref': 'daily', 'protected': True}, actor=actor,
        expected_revision=row['workflow']['definition_revision'], idempotency_key='protect')
    now[0] += 31 * 86400
    assert api.execute('workflows.cleanup.run', {}, actor=actor, idempotency_key='t')['counts']['recycled'] == 0
    row = api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    edited = api.execute('workflows.update', {'workflow_ref': 'daily', 'definition': definition(body_markdown='真实编辑')},
        actor=actor, expected_revision=row['workflow']['definition_revision'], idempotency_key='edit')
    assert edited['definition_revision'] > row['workflow']['definition_revision']
    assert api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)['cleanup_status']['orphan_since'] != row['cleanup_status']['orphan_since']
    policy = api.execute('workflows.cleanup.policy.get', {}, actor=actor)
    changed = api.execute('workflows.cleanup.policy.set', {'policy': {'enabled': False, 'mode': 'recycle', 'orphan_after_days': 90}},
        actor=actor, expected_revision=policy['revision'], idempotency_key='policy')
    assert changed['policy']['orphan_after_days'] == 90


def test_async_agent_yields_lock_and_resumes_same_attempt_without_replaying_prefix(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow

    api, actor, _ = service(tmp_path)
    submitted, executions = [], []
    completed = [False]
    bound = {}

    class BackgroundPort:
        def submit(self, request, *, actor, idempotency_key):
            assert request == {'space_id': 's1', 'instruction': '核对输入', 'controller_agent_id': 'controller'}
            pending = WorkflowRunStore(tmp_path).load_run(bound['run'])
            assert pending.steps[1].escalation_result['attempt_id']
            submitted.append(idempotency_key)
            return {'task_id': 'stask-exact', 'request_id': 'request-exact', 'operation_id': 'op', 'state': 'accepted'}

    def completion(task_id, **identity):
        if not completed[0]:
            return None
        return {**identity, 'task_id': task_id, 'status': 'completed', 'completion_ref': 'complete-1',
                'output': {'value': 7}, 'run_id': 'canonical-agent-run', 'result_ref': 'result-1'}

    steps = WorkflowAgentSteps(BackgroundPort(), actor_provider=lambda run: actor, completion_reader=completion)
    script(tmp_path)
    real = ScriptRunner.run_script

    def observe(self, script_id, *args, **kwargs):
        executions.append(script_id)
        return real(self, script_id, *args, **kwargs)

    monkeypatch.setattr(ScriptRunner, 'run_script', observe)
    context = BusinessExecutionContext('alice', 's1', None, 'stask-parent', 1, None, (), (), 'business.v1')
    flow = workflow(step(), step('agent', mode='agent_task', script_id='',
        config={'instruction': '核对输入', 'controller_agent_id': 'controller'}), step('tail'))
    bound['run'] = 'workflow-exact'
    engine = WorkflowEngine(tmp_path, agent_steps=steps, run_authorizer=lambda run, phase: None)
    first = engine.run_workflow(flow, business_context=context, workflow_run_id=bound['run'],
        scope_snapshot={'scope_mode': 'spaces', 'space_ids': ['s1'], 'scope_revision': 4})
    assert first.status == 'waiting_for_agent'
    assert first.business_context['task_id'] == 'stask-parent'
    assert first.scope_revision == 4 and executions == ['collect']
    attempt = first.steps[1].escalation_result['attempt_id']
    with WorkflowRunStore(tmp_path).execution_lock(first.workflow_run_id):
        pass  # 等待期间原执行锁确实可取得。
    restarted = WorkflowEngine(tmp_path, agent_steps=steps, run_authorizer=lambda run, phase: None)
    still_waiting = restarted.resume_workflow_run(first.workflow_run_id)
    assert still_waiting.status == 'waiting_for_agent' and len(submitted) == 1
    completed[0] = True
    done = restarted.resume_workflow_run(first.workflow_run_id)
    assert done.status == 'succeeded' and executions == ['collect', 'collect']
    assert done.steps[1].escalation_result['attempt_id'] == attempt
    assert done.steps[1].output_snapshot == {'value': 7}
    assert done.business_context == first.business_context and done.scope_snapshot == first.scope_snapshot
    assert restarted.resume_workflow_run(done.workflow_run_id).to_dict() == done.to_dict()
    stale = WorkflowRunRecord.from_dict(done.to_dict())
    restarted.store.save_run(done)
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        restarted.store.save_run(stale)


def test_async_submit_ack_loss_rechecks_current_gate_and_keeps_attempt(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import step, workflow

    calls, authorized = [], [True]
    actor = SimpleNamespace(owner_id='alice')

    class BackgroundPort:
        def submit(self, request, *, actor, idempotency_key):
            calls.append(idempotency_key)
            raise OSError('lost ACK')

    def authorize(run, phase):
        if phase == 'agent' and not authorized[0]:
            raise PermissionError('current effect permission revoked')

    agents = WorkflowAgentSteps(BackgroundPort(), actor_provider=lambda run: actor, completion_reader=lambda *a, **k: None)
    engine = WorkflowEngine(tmp_path, agent_steps=agents, run_authorizer=authorize)
    context = BusinessExecutionContext('alice', 's1', None, 'stask-1', 1, None, (), (), 'business.v1')
    first = engine.run_workflow(workflow(step('agent', mode='agent_task', script_id='',
        config={'instruction': '核对'})), business_context=context)
    assert first.status == 'waiting_for_agent' and len(calls) == 1
    authorized[0] = False
    with pytest.raises(PermissionError):
        engine.resume_workflow_run(first.workflow_run_id)
    assert len(calls) == 1
    authorized[0] = True
    second = engine.resume_workflow_run(first.workflow_run_id)
    assert second.status == 'waiting_for_agent' and len(calls) == 2 and calls[0] == calls[1]
