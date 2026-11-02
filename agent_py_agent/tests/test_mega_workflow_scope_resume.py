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


@pytest.fixture
def dispatch_contract(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchPlan
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / 'conversation')
    source = store.threads.create(owner_id='alice', owner_home=str(tmp_path))
    target = store.threads.create(owner_id='alice', owner_home=str(tmp_path))
    plan = SessionTaskDispatchPlan(source.thread_id, target.thread_id, '原输入', idempotency_key='original-operation')
    intent = BusinessContextIntent('alice', 'space-original', None, None)

    def factory(task_id):
        return BusinessExecutionContext('alice', 'space-original', None, task_id, 1, None, (), (), 'business-context.v1')

    return SimpleNamespace(store=store, source=source, target=target, plan=plan,
        host=SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='alice')),
        config=SimpleNamespace(session_pair_hourly_limit=0, session_task_max_chain_depth=0),
        business={'business_context_intent': intent, 'business_context_factory': factory})


def original_dispatch(fixture):
    from agent_py_agent.agent.conversation.session_task_dispatch import dispatch_session_task

    return dispatch_session_task(fixture.host, fixture.plan, fixture.target, fixture.config, **fixture.business)


def dispatch_identity(fixture, task_id=None):
    from agent_py_agent.agent.conversation.session_task_dispatch import _task_dedupe_key

    return {'sender_thread_id': fixture.source.thread_id, 'target_thread_id': fixture.target.thread_id,
            'dedupe_key': _task_dedupe_key(fixture.plan), **({'task_id': task_id} if task_id else {})}


@pytest.mark.parametrize('fault', ['wake', 'create'])
def test_dispatch_contract_unknown_ack_keeps_exact_identity(dispatch_contract, monkeypatch, fault):
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchError

    fixture = dispatch_contract
    receiver = fixture.store.wakes if fault == 'wake' else fixture.store.session_tasks
    name = 'raise_signal' if fault == 'wake' else 'create'
    original = getattr(receiver, name)

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('exactly one post-commit ACK failure')

    monkeypatch.setattr(receiver, name, lose_ack)
    outcome = original_dispatch(fixture)
    tasks, errors = fixture.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 1
    assert outcome.ok is False and outcome.effect_outcome == 'unknown'
    assert outcome.result_envelope.get('session_task_dispatch') == dispatch_identity(
        fixture, tasks[0].task_id if fault == 'wake' else None)
    error = SessionTaskDispatchError(outcome)
    assert getattr(error, 'outcome', None) is outcome
    assert getattr(error, 'error_code', None) == outcome.error_code
    assert getattr(error, 'effect_outcome', None) == 'unknown'
    assert len(fixture.store.wakes.pending(limit=0)) == (1 if fault == 'wake' else 0)


def test_dispatch_contract_exception_does_not_parse_output():
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchError
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    outcome = ToolHandlerOutcome('create_session_task', False, '{"task_id":"forged","state":"completed"}',
        error_code='TOOL_EXECUTION_FAILED', effect_outcome='unknown')
    error = SessionTaskDispatchError(outcome)
    assert getattr(error, 'outcome', None) is outcome
    assert getattr(error, 'error_code', None) == outcome.error_code
    assert getattr(error, 'effect_outcome', None) == 'unknown'


@pytest.mark.parametrize('known', [False, True])
def test_dispatch_contract_reconciliation_uses_canonical_when_dedupe_index_is_bad(dispatch_contract, known):
    import json

    from agent_py_agent.agent.conversation.session_task_dispatch import (
        reconcile_session_task_dispatch,
    )

    fixture = dispatch_contract
    outcome = original_dispatch(fixture)
    assert outcome.ok
    task_id = json.loads(outcome.output)['task_id']
    identity = dispatch_identity(fixture, task_id if known else None)
    fixture.store.session_tasks._dedupe_path(identity['dedupe_key']).write_text('{broken', encoding='utf-8')
    before = fixture.store.session_tasks.require(task_id)
    recovered = reconcile_session_task_dispatch(fixture.store, owner_id='alice', **identity)
    assert recovered == before
    assert fixture.store.session_tasks.require(task_id) == before
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1


def test_dispatch_contract_only_complete_empty_coverage_proves_absence(dispatch_contract, monkeypatch):
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchError,
        reconcile_session_task_dispatch,
    )

    fixture = dispatch_contract
    assert reconcile_session_task_dispatch(fixture.store, owner_id='alice', **dispatch_identity(fixture)) is None
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', lambda **kwargs: {
        'items': [], 'next_cursor': None, 'coverage': 'partial', 'load_errors': []})
    with pytest.raises(SessionTaskDispatchError) as unresolved:
        reconcile_session_task_dispatch(fixture.store, owner_id='alice', **dispatch_identity(fixture))
    assert unresolved.value.effect_outcome == 'unknown'


@pytest.mark.parametrize('mismatch', ['owner', 'sender', 'target', 'dedupe', 'task', 'business'])
def test_dispatch_contract_reconciliation_rejects_foreign_and_mismatched_identity(dispatch_contract, mismatch):
    import json

    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchError,
        reconcile_session_task_dispatch,
    )

    fixture = dispatch_contract
    task_id = json.loads(original_dispatch(fixture).output)['task_id']
    identity = dispatch_identity(fixture, task_id)
    owner = 'alice'
    if mismatch == 'owner':
        owner = 'bob'
    elif mismatch == 'sender':
        identity['sender_thread_id'] = fixture.target.thread_id
    elif mismatch == 'target':
        identity['target_thread_id'] = fixture.source.thread_id
    elif mismatch == 'dedupe':
        identity['dedupe_key'] = 'not-the-original-key'
    elif mismatch == 'task':
        identity['task_id'] = 'stask-not-the-original'
    else:
        path = fixture.store.session_tasks._path(task_id)
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['metadata']['business_context']['task_id'] = 'stask-foreign-link'
        path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises((PermissionError, SessionTaskDispatchError)):
        reconcile_session_task_dispatch(fixture.store, owner_id=owner, **identity)
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1


@pytest.fixture
def real_workflow_dispatch(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        _BusinessBackgroundService,
        bind_business_context,
    )
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow

    home = replace(home_paths(tmp_path / 'isolated-home'), owner_id='alice', owner_provider='local', owner_kind='main')
    store = ConversationStore(home.owner_home_dir / 'conversations')
    source = store.threads.create(owner_id=home.owner_id, canonical_user_id='local-test', cwd=str(home.owner_home_dir))
    host = SimpleNamespace(home_paths=home, conversation_store=store,
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'),
        run=lambda *a, **k: pytest.fail('cancellation/recovery must never call a model'))
    actor = HostChannelActor(home.owner_id, source.thread_id, 'original-entry', 'local', 'local', 'user')
    services = BusinessServices(host)
    authority = bind_business_context(host, authorize=lambda action, facts: True, actor=actor)
    space = services.spaces.create({'name': '原空间'}, actor=services.actor(authority), idempotency_key='original-space')
    background = _BusinessBackgroundService(services, authority)
    executions, submissions, cancellations = [], [], []
    original_script = ScriptRunner.run_script
    original_submit = background.submit

    def observe_script(self, script_id, *args, **kwargs):
        executions.append(script_id)
        return original_script(self, script_id, *args, **kwargs)

    def observe_submit(request, *, actor, idempotency_key):
        submissions.append(idempotency_key)
        return original_submit(request, actor=actor, idempotency_key=idempotency_key)

    def cancel(task_id, *, actor, idempotency_key):
        current = engine.store.load_run('workflow-original')
        assert current.steps[1].escalation_result['task_id'] == task_id
        with pytest.raises(RuntimeError):
            with engine.store.execution_lock('workflow-original'):
                pytest.fail('the original workflow execution lock must still be held')
        receipt = background.cancel_task(task_id, actor=actor, include_descendants=True)
        cancellations.append((idempotency_key, receipt))
        return receipt

    monkeypatch.setattr(ScriptRunner, 'run_script', observe_script)
    monkeypatch.setattr(background, 'submit', observe_submit)
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor,
        completion_reader=lambda *a, **k: None, cancel_handler=cancel)
    engine = WorkflowEngine(home.owner_home_dir, agent_steps=agents, run_authorizer=lambda run, phase: None)
    script(home.owner_home_dir, owner_id=home.owner_id)
    flow = workflow(step(), step('agent', mode='agent_task', script_id='', config={'instruction': '核对原输入'}), step('tail'))
    flow['descriptor']['owner_id'] = home.owner_id
    context = BusinessExecutionContext(home.owner_id, space['space_id'], None, 'stask-parent',
        space['revision'], None, (), (), 'business-context.v1')

    def start():
        return engine.run_workflow(flow, workflow_run_id='workflow-original', business_context=context,
            scope_snapshot={'scope_mode': 'spaces', 'space_ids': [space['space_id']], 'scope_revision': 4})

    return SimpleNamespace(store=store, background=background, engine=engine, start=start, actor=actor,
        executions=executions, submissions=submissions, cancellations=cancellations)


def assert_original_workflow_cancelled(fixture, first, *, expected_revision=None):
    engine = fixture.engine
    expected = first.revision if expected_revision is None else expected_revision
    outstanding = engine.cancel_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='cancel-original')
    current = engine.store.load_run(first.workflow_run_id)
    assert outstanding == [] and current.status == 'cancelled'
    assert current.workflow_run_id == first.workflow_run_id
    assert current.steps[0] == first.steps[0] and current.steps[2] == first.steps[2]
    assert current.steps[1].escalation_result['attempt_id'] == first.steps[1].escalation_result['attempt_id']
    assert current.business_context == first.business_context and current.scope_snapshot == first.scope_snapshot
    assert fixture.executions == ['collect'] and len(fixture.submissions) == 1
    assert fixture.cancellations[-1][1]['state'] == 'completed'
    assert fixture.cancellations[-1][1]['outstanding_refs'] == []
    operation = engine.store._read(first.workflow_run_id, 'run.json')['control_operations']['cancel-original']
    assert operation['state'] == 'completed'
    before_replay = current.to_dict()
    assert engine.cancel_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='cancel-original') == []
    assert engine.store.load_run(first.workflow_run_id).to_dict() == before_replay
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        engine.resume_workflow_run(first.workflow_run_id)
    assert fixture.executions == ['collect'] and len(fixture.submissions) == 1
    return current


def test_real_workflow_dispatch_accepts_original_w2_completed_cancellation(real_workflow_dispatch):
    fixture = real_workflow_dispatch
    first = fixture.start()
    assert first.status == 'waiting_for_agent'
    task_id = first.steps[1].escalation_result['task_id']
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1
    assert_original_workflow_cancelled(fixture, first)
    assert fixture.store.session_tasks.require(task_id).status == 'cancelled'
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1


def test_real_workflow_dispatch_pending_cancel_converges_on_original_control_key(real_workflow_dispatch, monkeypatch):
    fixture = real_workflow_dispatch
    first = fixture.start()
    task_id = first.steps[1].escalation_result['task_id']
    original = fixture.store.guidance.mark_status

    def blocked_cleanup(*args, **kwargs):
        raise OSError('original queued input cleanup temporarily unconfirmed')

    monkeypatch.setattr(fixture.store.guidance, 'mark_status', blocked_cleanup)
    outstanding = fixture.engine.cancel_workflow_run(first.workflow_run_id,
        expected_revision=first.revision, operation_key='cancel-original')
    assert task_id in outstanding
    pending = fixture.engine.store.load_run(first.workflow_run_id)
    assert pending.status == 'cancelling'
    assert fixture.cancellations[-1][1]['state'] == 'pending'
    assert fixture.cancellations[-1][1]['outstanding_refs']
    assert fixture.engine.store._read(first.workflow_run_id, 'run.json')['control_operations']['cancel-original']['state'] == 'pending'
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        fixture.engine.resume_workflow_run(first.workflow_run_id)
    monkeypatch.setattr(fixture.store.guidance, 'mark_status', original)
    final = assert_original_workflow_cancelled(fixture, first)
    assert final.status == 'cancelled' and len(fixture.cancellations) == 2
    assert fixture.cancellations[0][0] == fixture.cancellations[1][0]
    assert fixture.store.session_tasks.require(task_id).status == 'cancelled'


@pytest.mark.parametrize('fault', ['wake', 'create'])
@pytest.mark.parametrize('label', ['waiting_for_agent', 'failed', 'paused_for_resume'])
def test_real_workflow_dispatch_unknown_ack_cancels_same_original_task(real_workflow_dispatch, monkeypatch, fault, label, record_property):
    import json

    fixture = real_workflow_dispatch
    receiver = fixture.store.wakes if fault == 'wake' else fixture.store.session_tasks
    name = 'raise_signal' if fault == 'wake' else 'create'
    original = getattr(receiver, name)
    faults = []

    def lose_one_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if not faults:
            faults.append('committed')
            raise OSError('one durable dispatch ACK loss')
        return result

    monkeypatch.setattr(receiver, name, lose_one_ack)
    first = fixture.start()
    assert first.status == 'waiting_for_agent'
    result = first.steps[1].escalation_result
    assert result['submission_state'] == 'unknown'
    tasks, errors = fixture.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 1 and faults == ['committed']
    task = tasks[0]
    expected_identity = {'sender_thread_id': task.sender_thread_id, 'target_thread_id': task.target_thread_id,
        'dedupe_key': task.dedupe_key, **({'task_id': task.task_id} if fault == 'wake' else {})}
    assert result['session_task_dispatch'] == expected_identity
    assert result.get('task_id') == (task.task_id if fault == 'wake' else None)
    initial_wakes = len(fixture.store.wakes.pending(limit=0))
    assert initial_wakes == (1 if fault == 'wake' else 0)
    if label != 'waiting_for_agent':
        first.status = first.steps[1].status = label
        fixture.engine.store.save_run(first)
    final = assert_original_workflow_cancelled(fixture, first)
    assert final.steps[1].escalation_result['task_id'] == task.task_id
    tasks, errors = fixture.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 1 and tasks[0].status == 'cancelled'
    assert len(fixture.store.wakes.pending(limit=0)) == initial_wakes
    assert len(fixture.cancellations) == 1 and len(fixture.submissions) == 1
    record_property('cancellation_evidence', json.dumps({'fault': fault, 'label': label,
        'initial_tasks': 1, 'final_tasks': len(tasks), 'initial_wakes': initial_wakes,
        'final_wakes': len(fixture.store.wakes.pending(limit=0)), 'task_status': tasks[0].status,
        'workflow_status': final.status, 'outstanding_refs': fixture.cancellations[-1][1]['outstanding_refs'],
        'control_replay': 'stable', 'submissions': len(fixture.submissions)}))


@pytest.mark.parametrize('label', ['waiting_for_agent', 'failed', 'paused_for_resume'])
def test_real_workflow_dispatch_recovery_never_resubmits_or_runs_suffix(real_workflow_dispatch, monkeypatch, label):
    fixture = real_workflow_dispatch
    original = fixture.store.session_tasks.create
    faults = []

    def lose_one_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if not faults:
            faults.append('committed')
            raise OSError('canonical creation committed without returning the task ID')
        return result

    monkeypatch.setattr(fixture.store.session_tasks, 'create', lose_one_ack)
    first = fixture.start()
    assert first.status == 'waiting_for_agent'
    task = fixture.store.session_tasks.list_report(limit=0)[0][0]
    if label != 'waiting_for_agent':
        first.status = first.steps[1].status = label
        fixture.engine.store.save_run(first)
    recovered = fixture.engine.resume_workflow_run(first.workflow_run_id)
    assert recovered.status == 'waiting_for_agent'
    assert recovered.steps[1].escalation_result['task_id'] == task.task_id
    assert recovered.steps[1].escalation_result['attempt_id'] == first.steps[1].escalation_result['attempt_id']
    assert len(fixture.submissions) == 1 and fixture.executions == ['collect']
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert fixture.store.wakes.pending(limit=0) == []


@pytest.mark.parametrize('label', ['failed', 'paused_for_resume'])
def test_real_workflow_dispatch_cancel_keeps_known_task_obligation_despite_label(real_workflow_dispatch, label):
    fixture = real_workflow_dispatch
    first = fixture.start()
    task_id = first.steps[1].escalation_result['task_id']
    first.status = first.steps[1].status = label
    fixture.engine.store.save_run(first)
    fixture.engine.cancel_workflow_run(first.workflow_run_id, expected_revision=first.revision, operation_key='cancel-original')
    assert fixture.store.session_tasks.require(task_id).status == 'cancelled'
    assert fixture.engine.store.load_run(first.workflow_run_id).status == 'cancelled'
    assert len(fixture.cancellations) == 1 and len(fixture.submissions) == 1
    assert fixture.executions == ['collect']


@pytest.mark.parametrize('label', ['failed', 'paused_for_resume'])
def test_real_workflow_dispatch_resume_existing_task_preserves_attempt_and_prefix(real_workflow_dispatch, label):
    fixture = real_workflow_dispatch
    first = fixture.start()
    first.status = first.steps[1].status = label
    fixture.engine.store.save_run(first)
    resumed = fixture.engine.resume_workflow_run(first.workflow_run_id)
    assert resumed.steps[1].escalation_result['attempt_id'] == first.steps[1].escalation_result['attempt_id']
    assert resumed.steps[1].escalation_result['task_id'] == first.steps[1].escalation_result['task_id']
    assert resumed.status == 'waiting_for_agent' and len(fixture.submissions) == 1
    assert resumed.steps[0] == first.steps[0] and resumed.steps[2] == first.steps[2]
    assert fixture.executions == ['collect'] and len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1


def test_real_workflow_dispatch_rejects_foreign_source_before_submission(real_workflow_dispatch):
    from dataclasses import replace

    fixture = real_workflow_dispatch
    foreign = fixture.store.threads.create(owner_id='bob', canonical_user_id='foreign')
    fixture.engine.agent_steps.actor_provider = lambda run: replace(fixture.actor, source_thread_id=foreign.thread_id)
    first = fixture.start()
    assert first.status == 'failed'
    assert fixture.store.session_tasks.list_report(limit=0) == ([], [])
    assert fixture.store.wakes.pending(limit=0) == []


@pytest.mark.parametrize('field', ['sender_thread_id', 'target_thread_id', 'dedupe_key', 'task_id'])
def test_real_workflow_dispatch_cancel_rejects_mismatched_identity_before_effects(real_workflow_dispatch, field):
    fixture = real_workflow_dispatch
    first = fixture.start()
    task = fixture.store.session_tasks.require(first.steps[1].escalation_result['task_id'])
    identity = {name: getattr(task, name) for name in ('sender_thread_id', 'target_thread_id', 'dedupe_key', 'task_id')}
    identity[field] = {'sender_thread_id': task.target_thread_id, 'target_thread_id': task.sender_thread_id,
        'dedupe_key': 'not-original-dedupe', 'task_id': 'stask-not-original'}[field]
    first.steps[1].escalation_result['session_task_dispatch'] = identity
    fixture.engine.store.save_run(first)
    with pytest.raises(PermissionError):
        fixture.engine.cancel_workflow_run(first.workflow_run_id, expected_revision=first.revision, operation_key='cancel-original')
    assert fixture.store.session_tasks.require(task.task_id) == task
    assert fixture.engine.store.load_run(first.workflow_run_id).status == 'cancelling'
    assert fixture.cancellations == [] and len(fixture.submissions) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1 and fixture.executions == ['collect']


@pytest.mark.parametrize('receipt', [None, {}, {'state': 'completed'},
    {'state': 'completed', 'outstanding_refs': ['unconfirmed']},
    {'state': 'pending', 'outstanding_refs': []}, {'state': 'cancelled', 'outstanding_refs': []}])
def test_real_workflow_dispatch_unconfirmed_receipt_never_completes_control(real_workflow_dispatch, receipt):
    fixture = real_workflow_dispatch
    first = fixture.start()
    original_handler = fixture.engine.agent_steps.cancel_handler

    def unreadable_ack(*args, **kwargs):
        original_handler(*args, **kwargs)
        return receipt

    fixture.engine.agent_steps.cancel_handler = unreadable_ack
    outstanding = fixture.engine.cancel_workflow_run(first.workflow_run_id,
        expected_revision=first.revision, operation_key='cancel-original')
    assert outstanding == [first.steps[1].escalation_result['task_id']]
    assert fixture.engine.store.load_run(first.workflow_run_id).status == 'cancelling'
    assert fixture.engine.store._read(first.workflow_run_id, 'run.json')['control_operations']['cancel-original']['state'] == 'pending'
    fixture.engine.agent_steps.cancel_handler = original_handler
    final = assert_original_workflow_cancelled(fixture, first)
    assert final.status == 'cancelled' and fixture.cancellations[0][0] == fixture.cancellations[1][0]


def test_real_workflow_dispatch_partial_canonical_recovery_stays_pending_without_replay(real_workflow_dispatch, monkeypatch):
    fixture = real_workflow_dispatch
    first = fixture.start()
    task = fixture.store.session_tasks.require(first.steps[1].escalation_result.pop('task_id'))
    first.steps[1].escalation_result.update(submission_state='unknown', session_task_dispatch={
        name: getattr(task, name) for name in ('sender_thread_id', 'target_thread_id', 'dedupe_key')})
    fixture.engine.store.save_run(first)
    fixture.store.session_tasks._dedupe_path(task.dedupe_key).write_text('{broken', encoding='utf-8')
    original_page = fixture.store.session_tasks.list_page
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', lambda **kwargs: {
        'items': [], 'next_cursor': None, 'coverage': 'partial', 'load_errors': []})
    outstanding = fixture.engine.cancel_workflow_run(first.workflow_run_id,
        expected_revision=first.revision, operation_key='cancel-original')
    pending = fixture.engine.store.load_run(first.workflow_run_id)
    assert outstanding == [first.steps[1].escalation_result['attempt_id']]
    assert pending.status == 'cancelling' and not pending.steps[1].escalation_result.get('task_id')
    assert fixture.cancellations == [] and len(fixture.submissions) == 1
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', original_page)
    final = assert_original_workflow_cancelled(fixture, first)
    assert final.steps[1].escalation_result['task_id'] == task.task_id
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1 and len(fixture.submissions) == 1


# LLM: 使用真实canonical文件制造同dedupe坏账；保留有效索引，不替换派发/取消端口。
# 函数用途: 验证无ID恢复不能只凭索引选中一个冲突任务并漏掉另一个。
def test_real_workflow_dispatch_index_hit_cannot_hide_canonical_conflict(real_workflow_dispatch):
    import json

    fixture = real_workflow_dispatch
    first = fixture.start()
    result = first.steps[1].escalation_result
    task = fixture.store.session_tasks.require(result.pop('task_id'))
    result.update(submission_state='unknown', session_task_dispatch={
        name: getattr(task, name) for name in ('sender_thread_id', 'target_thread_id', 'dedupe_key')})
    fixture.engine.store.save_run(first)
    duplicate = task.to_dict()
    duplicate['task_id'] = 'stask-canonical-conflict'
    duplicate['metadata']['business_context']['task_id'] = duplicate['task_id']
    path = fixture.store.session_tasks._path(duplicate['task_id'])
    path.write_text(json.dumps(duplicate), encoding='utf-8')
    fixture.store.session_tasks._list_index().publish(path)
    assert fixture.store.session_tasks.find_by_dedupe_key(task.dedupe_key).task_id == task.task_id
    with pytest.raises(PermissionError, match='IDENTITY_CONFLICT'):
        fixture.engine.cancel_workflow_run(first.workflow_run_id,
            expected_revision=first.revision, operation_key='cancel-original')
    current = fixture.engine.store.load_run(first.workflow_run_id)
    assert current.status == 'cancelling' and not current.steps[1].escalation_result.get('task_id')
    assert fixture.engine.store._read(first.workflow_run_id, 'run.json')['control_operations']['cancel-original']['state'] == 'pending'
    tasks, errors = fixture.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 2 and all(item.status == 'queued' for item in tasks)
    assert fixture.cancellations == [] and len(fixture.submissions) == 1 and fixture.executions == ['collect']
    assert len(fixture.store.wakes.pending(limit=0)) == 1


# LLM: 索引仍能精确命中原canonical，但分页覆盖被注入为partial；恢复不能跳过覆盖检查。
# 函数用途: 验证有效索引不能掩盖不完整的责任范围，原键重试可在完整覆盖后收敛。
def test_real_workflow_dispatch_index_hit_requires_complete_coverage(real_workflow_dispatch, monkeypatch):
    fixture = real_workflow_dispatch
    first = fixture.start()
    result = first.steps[1].escalation_result
    task = fixture.store.session_tasks.require(result.pop('task_id'))
    result.update(submission_state='unknown', session_task_dispatch={
        name: getattr(task, name) for name in ('sender_thread_id', 'target_thread_id', 'dedupe_key')})
    fixture.engine.store.save_run(first)
    assert fixture.store.session_tasks.find_by_dedupe_key(task.dedupe_key).task_id == task.task_id
    original_page = fixture.store.session_tasks.list_page
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', lambda **kwargs: {
        'items': [task], 'next_cursor': None, 'coverage': 'partial', 'load_errors': []})
    outstanding = fixture.engine.cancel_workflow_run(first.workflow_run_id,
        expected_revision=first.revision, operation_key='cancel-original')
    current = fixture.engine.store.load_run(first.workflow_run_id)
    assert outstanding == [result['attempt_id']] and current.status == 'cancelling'
    assert not current.steps[1].escalation_result.get('task_id') and fixture.cancellations == []
    assert fixture.store.session_tasks.require(task.task_id) == task
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', original_page)
    assert_original_workflow_cancelled(fixture, first)
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1


# LLM: 真适配器正常耐久提交后在返回边界丢失旧式ACK；不提供伪造typed身份或替换派发实现。
# 函数用途: 确认真实Store上的无身份unknown不能靠再次调用submit找回任务。
@pytest.mark.parametrize('label', ['waiting_for_agent', 'failed', 'paused_for_resume'])
def test_real_workflow_dispatch_untyped_unknown_never_reenters_submit(real_workflow_dispatch, monkeypatch, label):
    fixture = real_workflow_dispatch
    original_submit = fixture.background.submit

    def lose_adapter_ack(*args, **kwargs):
        original_submit(*args, **kwargs)
        raise OSError('real adapter committed but caller received no structured ACK')

    monkeypatch.setattr(fixture.background, 'submit', lose_adapter_ack)
    first = fixture.start()
    result = first.steps[1].escalation_result
    assert result['submission_state'] == 'unknown'
    assert 'session_task_dispatch' not in result and not result.get('task_id')
    first.status = first.steps[1].status = label
    fixture.engine.store.save_run(first)
    task = fixture.store.session_tasks.list_report(limit=0)[0][0]
    before = fixture.engine.store.load_run(first.workflow_run_id)
    recovered = fixture.engine.resume_workflow_run(first.workflow_run_id)
    current = recovered.steps[1].escalation_result
    assert recovered.status == 'waiting_for_agent' and current['submission_state'] == 'unknown'
    assert current['attempt_id'] == result['attempt_id'] and current['submission_key'] == result['submission_key']
    assert 'session_task_dispatch' not in current and not current.get('task_id')
    assert recovered.steps[0] == before.steps[0] and recovered.steps[2] == before.steps[2]
    assert len(fixture.submissions) == 1 and fixture.executions == ['collect']
    assert fixture.store.session_tasks.require(task.task_id) == task
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1
    outstanding = fixture.engine.cancel_workflow_run(first.workflow_run_id,
        expected_revision=recovered.revision, operation_key='cancel-original')
    assert outstanding == [result['attempt_id']] and fixture.cancellations == []
    assert fixture.engine.store.load_run(first.workflow_run_id).status == 'cancelling'


# LLM: 只在真实Store已原子发表步骤快照后注入一次IO错误，保留run旧状态；不用伪Store代替恢复。
# 函数用途: 验证完成快照与run状态间的崩溃不会卡死、重派任务或重放前缀/后缀。
@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_real_workflow_dispatch_completion_snapshot_crash_recovers_without_effects(real_workflow_dispatch, monkeypatch, status):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    fixture = real_workflow_dispatch
    first = fixture.start()
    task_id = first.steps[1].escalation_result['task_id']
    fixture.engine.agent_steps.completion_reader = lambda task_id, **identity: {
        **identity, 'task_id': task_id, 'status': status, 'completion_ref': 'canonical-completion',
        'output': {'value': 7}, 'run_id': 'canonical-agent-run', 'result_ref': 'canonical-result'}
    original_write = fixture.engine.store._write_step
    faults = []

    def lose_step_ack(workflow_run_id, step):
        original_write(workflow_run_id, step)
        if step.step_id == 'agent' and step.status in ('succeeded', 'failed') and not faults:
            faults.append('snapshot-published')
            raise OSError('completion snapshot durable before workflow status acknowledgement')

    monkeypatch.setattr(fixture.engine.store, '_write_step', lose_step_ack)
    with pytest.raises(OSError, match='completion snapshot durable'):
        fixture.engine.resume_workflow_run(first.workflow_run_id)
    crashed = fixture.engine.store.load_run(first.workflow_run_id)
    assert crashed.status == 'waiting_for_agent' and faults == ['snapshot-published']
    assert crashed.steps[1].escalation_result['completion_ref'] == 'canonical-completion'
    fixture.engine.agent_steps.completion_reader = lambda *a, **k: pytest.fail('durable completion must not be re-read')
    restarted = WorkflowEngine(fixture.engine.owner_home, agent_steps=fixture.engine.agent_steps,
        run_authorizer=fixture.engine.run_authorizer)
    recovered = restarted.resume_workflow_run(first.workflow_run_id, expected_revision=crashed.revision,
        operation_key='resume-recover')
    assert recovered.status == ('running' if status == 'completed' else 'failed')
    assert recovered.steps[1] == crashed.steps[1] and recovered.steps[0] == first.steps[0]
    assert recovered.steps[2] == first.steps[2]
    assert recovered.business_context == first.business_context and recovered.scope_snapshot == first.scope_snapshot
    assert len(fixture.submissions) == 1 and fixture.executions == ['collect']
    assert fixture.store.session_tasks.require(task_id).status == 'queued'
    assert len(fixture.store.session_tasks.list_report(limit=0)[0]) == 1
    assert len(fixture.store.wakes.pending(limit=0)) == 1
    before_replay = recovered.to_dict()
    assert restarted.resume_workflow_run(first.workflow_run_id, expected_revision=crashed.revision,
        operation_key='resume-recover').to_dict() == before_replay
    assert restarted.store.load_run(first.workflow_run_id).to_dict() == before_replay
    assert len(fixture.submissions) == 1 and fixture.executions == ['collect']
