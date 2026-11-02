"""业务动作只经原 WorkflowStore；范围/正文/幂等回执同次发表。"""
import errno
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation.business_contracts import WorkflowScope


def definition(name='daily', **changes):
    return {'descriptor': {'id': name, 'kind': 'workflow', 'name': name,
            'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': [], **changes}


def service(home, *, references=None, clock=None):
    from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
    actor = SimpleNamespace(owner_id='alice')
    authorized = {'value': True}
    instance = WorkflowScopeService(home, owner_id='alice',
        authorize=lambda current, action, ref: current is actor and authorized['value'],
        reference_reader=references, clock=clock)
    return instance, actor, authorized


def test_create_scope_same_commit_and_idempotency(tmp_path):
    api, actor, _ = service(tmp_path)
    request = {'definition': definition(), 'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}
    first = api.execute('workflows.create', request, actor=actor, idempotency_key='create')
    assert first['scope'] == request['scope']
    assert api.execute('workflows.create', request, actor=actor, idempotency_key='create') == first
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        api.execute('workflows.create', {**request, 'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
                    actor=actor, idempotency_key='create')
    assert len(WorkflowStore(tmp_path).list_workflows()) == 1
    assert api.execute('workflows.list', {'space_id': 'other'}, actor=actor)['items'] == []
    assert len(api.execute('workflows.list', {'space_id': 's1'}, actor=actor)['items']) == 1


def test_create_failure_never_exposes_global(tmp_path, monkeypatch):
    import agent_py_agent.agent.assets as assets
    api, actor, _ = service(tmp_path)
    real = assets.write_text_atomic_beneath

    def fail(root, parts, text, **options):
        if parts[-1] == 'workflow.json':
            raise OSError(errno.ENOSPC, 'single canonical write failure')
        return real(root, parts, text, **options)

    with monkeypatch.context() as patch:
        patch.setattr(assets, 'write_text_atomic_beneath', fail)
        with pytest.raises(OSError):
            api.execute('workflows.create', {'definition': definition(),
                'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}, actor=actor, idempotency_key='once')
    assert WorkflowStore(tmp_path).get_workflow('daily') is None
    result = api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}}, actor=actor, idempotency_key='once')
    assert result['scope']['scope_mode'] == 'spaces'


def test_scope_cas_does_not_change_execution_definition(tmp_path):
    api, actor, authorized = service(tmp_path)
    api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='create')
    before = WorkflowStore(tmp_path).get_workflow('daily').execution_definition_ref
    current = api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=actor)
    changed = api.set_scope('daily', WorkflowScope('unassigned', (), current['scope_revision']),
                            actor=actor, expected_revision=current['scope_revision'], idempotency_key='scope')
    assert changed['scope']['scope_mode'] == 'unassigned'
    assert WorkflowStore(tmp_path).get_workflow('daily').execution_definition_ref == before
    assert api.set_scope('daily', WorkflowScope('unassigned', (), current['scope_revision']),
        actor=actor, expected_revision=current['scope_revision'], idempotency_key='scope') == changed
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        api.set_scope('daily', WorkflowScope('owner_global', (), 0), actor=actor,
                      expected_revision=current['scope_revision'], idempotency_key='stale')
    authorized['value'] = False
    with pytest.raises(PermissionError):
        api.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)


def test_legacy_workflow_global_and_read_only_preview(tmp_path):
    api, actor, _ = service(tmp_path)
    WorkflowStore(tmp_path).save_workflow(definition())
    scope = api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=actor)
    assert scope['scope'] == {'scope_mode': 'owner_global', 'space_ids': []}
    before = WorkflowStore(tmp_path).get_workflow('daily').to_dict()
    preview = api.execute('workflows.preview', {'workflow_ref': 'daily'}, actor=actor)
    assert preview['errors'] == [] and preview['plan'] == []
    assert before == WorkflowStore(tmp_path).get_workflow('daily').to_dict()


def test_unassigned_rejects_run_and_foreign_actor(tmp_path):
    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='c')
    with pytest.raises(PermissionError):
        api.execute('workflows.run', {'workflow_ref': 'daily'}, actor=actor, idempotency_key='run')
    with pytest.raises(PermissionError):
        api.execute('workflows.scope.get', {'workflow_ref': 'daily'}, actor=SimpleNamespace(owner_id='bob'))
    assert not (tmp_path / 'data' / 'workflow_runs').exists()


# LLM: 治理授权须先于真实脚本副作用；测试只授予 effects 写根，不放宽生产沙箱或原断言。
# 函数用途: 验证治理拒绝、恢复与结果核对期间，真实脚本副作用只发生一次。
@pytest.mark.parametrize('invalid', ['candidate', 'self_review', 'revoked', 'attempt', 'unknown_effect', 'committed_resolution'])
def test_governance_consumed_before_real_script_effect(tmp_path, invalid):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.governance import (
        WorkflowGovernance,
        workflow_candidate_hash,
    )
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow

    store = WorkflowRunStore(tmp_path)
    allowed = [invalid != 'revoked']
    broken = [True]
    effects = tmp_path / 'effects'
    effects.mkdir()
    counter = effects / 'effect'
    script(tmp_path, f'from pathlib import Path\np=Path({str(counter)!r})\np.write_text((p.read_text() if p.exists() else "")+"executed")\nprint(\'{{"value":1}}\')')

    def receipt(ref, record, step_id):
        target = next(item for item in record.steps if item.step_id == step_id)
        value = {'receipt_ref': ref, 'owner_id': 'alice', 'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context.get('task_id', ''), 'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'], 'candidate_hash': workflow_candidate_hash(
                store.load_workflow_snapshot(record.workflow_run_id)), 'author_id': 'writer',
            'reviewer_id': 'independent-reviewer', 'decision': 'approved', 'expires_at': 9999999999}
        if broken[0] and invalid == 'candidate':
            value['candidate_hash'] = '0' * 64
        if broken[0] and invalid == 'self_review':
            value['reviewer_id'] = 'writer'
        if broken[0] and invalid == 'attempt':
            value['attempt_id'] = 'other-attempt'
        return value

    def reconcile(record, target):
        return {'state': 'committed' if invalid == 'committed_resolution' else 'unknown',
            'resolution_ref': 'resolution-1', 'owner_id': record.owner_id,
            'workflow_run_id': record.workflow_run_id, 'task_id': record.business_context.get('task_id', ''),
            'step_id': target.step_id, 'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id)),
            'output': {'value': 1}}

    guard = WorkflowGovernance(store, policy_provider=lambda record, phase: {
        'author_id': 'writer', 'review_refs': ['review-1'], 'minimum_reviews': 1},
        receipt_reader=receipt, authorization_check=lambda receipt, record: allowed[0],
        effect_reader=reconcile)
    engine = WorkflowEngine(tmp_path, run_authorizer=guard.require_effect)
    from agent_py_agent.agent.workflows import ScriptRunner
    engine.runner = ScriptRunner(tmp_path, extra_write_roots=(effects,))
    first = engine.run_workflow(workflow(step()))
    if invalid in ('unknown_effect', 'committed_resolution'):
        # 已开始/结果未知的旧尝试只能核对，不允许凭新的“批准”重跑。
        assert first.status == 'succeeded'
        first.status, first.effect_outcome = 'paused_for_resume', 'unknown'
        first.steps[0].status = 'paused_for_resume'
        store.save_run(first)
        if invalid == 'unknown_effect':
            with pytest.raises(PermissionError, match='RECONCILIATION'):
                engine.resume_workflow_run(first.workflow_run_id)
        else:
            reconciled = engine.resume_workflow_run(first.workflow_run_id)
            assert reconciled.status == 'succeeded' and reconciled.steps[0].escalation_result['resolution_origin'] == 'system'
        assert counter.read_text() == 'executed'
    else:
        assert first.status == 'failed' and not counter.exists()
        broken[0], allowed[0] = False, True
        done = engine.resume_workflow_run(first.workflow_run_id)
        assert done.status == 'succeeded' and counter.read_text() == 'executed'
        assert done.steps[0].escalation_result['review_refs'] == ['review-1']


def test_native_tool_consumes_business_service_and_preserves_host_engine(tmp_path):
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step

    api, actor, authorized = service(tmp_path)
    api.context_provider = lambda current, request: BusinessExecutionContext(
        'alice', 's1', None, 'stask-original', 1, None, (), (), 'business.v1')
    engine = WorkflowEngine(tmp_path)
    original_runner = engine.runner
    tool = WorkflowsTool(tmp_path, engine=engine)
    tool.business_service, tool.business_actor_provider = api, lambda: actor
    script(tmp_path)
    created = tool.execute({'action': 'workflows.create', 'definition': definition(steps=[step()]),
        'scope': {'scope_mode': 'spaces', 'space_ids': ['s1']}, 'idempotency_key': 'native-create'})
    assert created.ok, created.output
    assert created.result_envelope['scope']['space_ids'] == ['s1']
    started = tool.execute({'action': 'workflows.run', 'workflow_ref': 'daily', 'space_id': 's1',
                            'idempotency_key': 'native-run'})
    assert started.ok, started.output
    saved = engine.store.load_run(started.result_envelope['workflow_run_id'])
    assert saved.status == 'succeeded' and saved.business_context['task_id'] == 'stask-original'
    assert saved.scope_revision == 1 and saved.final_output == {'value': 1}
    assert engine.runner is original_runner and engine.run_authorizer is None
    replay = tool.execute({'action': 'workflows.run', 'workflow_ref': 'daily', 'space_id': 's1',
                           'idempotency_key': 'native-run'})
    assert replay.ok and replay.result_envelope == started.result_envelope
    authorized['value'] = False
    denied = tool.execute({'action': 'workflows.get', 'workflow_ref': 'daily'})
    assert not denied.ok and denied.effect_outcome == 'not_started'
    injected = tool.execute({'action': 'workflows.get', 'workflow_ref': 'daily', 'owner_id': 'alice'})
    assert not injected.ok and injected.error_code == 'TOOL_INVALID_ARGUMENTS'


def test_completed_control_replays_original_receipt_after_later_control(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow
    script(tmp_path)
    engine = WorkflowEngine(tmp_path)
    first = engine.run_workflow(workflow(step()))
    expected = first.revision
    resumed = engine.resume_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='resume-once')
    engine.cancel_workflow_run(first.workflow_run_id, expected_revision=resumed.revision, operation_key='cancel-once')
    replay = engine.resume_workflow_run(first.workflow_run_id, expected_revision=expected, operation_key='resume-once')
    assert replay.to_dict() == resumed.to_dict()
    assert engine.store.load_run(first.workflow_run_id).status == 'cancelled'


def test_legacy_delete_cannot_bypass_business_recycle_protection(tmp_path):
    api, actor, _ = service(tmp_path)
    api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='c')
    with pytest.raises(PermissionError, match='RECYCLE_REQUIRED'):
        WorkflowStore(tmp_path).delete_workflow('daily')
    assert WorkflowStore(tmp_path).get_workflow('daily') is not None


def test_real_script_sees_durable_admission_before_process_effect(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow
    record = tmp_path / 'data' / 'workflow_runs' / 'exact-running' / 'run.json'
    script(tmp_path, f'import json\nfrom pathlib import Path\nr=json.loads(Path({str(record)!r}).read_text())\n'
                     'print(json.dumps({"value":r["effect_outcome"]}))')
    done = WorkflowEngine(tmp_path).run_workflow(workflow(step()), workflow_run_id='exact-running')
    assert done.status == 'succeeded' and done.final_output == {'value': 'unknown'}
