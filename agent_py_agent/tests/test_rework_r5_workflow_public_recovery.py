"""P1-R5-03：公开恢复只读原生任务完成链，不把未知副作用当作重新派发许可。"""
from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import session_tasks as task_records
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _BusinessBackgroundService,
    bind_business_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows import engine as engine_module
from agent_py_agent.agent.workflows import run_store as run_records
from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowGovernance, workflow_candidate_hash
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord, payload
from agent_py_agent.agent.workflows.run_store import (
    WorkflowExecutionError,
    WorkflowRunStore,
    execution_receipt,
)
from agent_py_agent.agent.workflows.script_runner import ScriptRunner


@pytest.fixture(params=[False, True], ids=['plain', 'governed'])
def public_recovery(tmp_path, monkeypatch, request):
    """真实领域服务/引擎/双 Store/适配器；只观察调用，模型和脚本不得启动。"""
    assert Path(engine_module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    home = replace(home_paths(tmp_path / 'home'), owner_id='alice', owner_provider='local', owner_kind='main')
    store = ConversationStore(home.owner_home_dir / 'conversations')
    source = store.threads.create(owner_id='alice', canonical_user_id='original-user')
    actor = HostChannelActor('alice', source.thread_id, 'original-entry', 'local', 'local', 'user')
    access = {'allowed': True, 'denied': set(), 'review_allowed': True}
    host = SimpleNamespace(home_paths=home, conversation_store=store,
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'),
        run=lambda *a, **k: pytest.fail('workflow recovery must not run a model'))
    services = BusinessServices(host)
    authority = bind_business_context(host, actor=actor, authorize=lambda action, facts: access['allowed'])
    space = services.spaces.create({'name': '原空间'}, actor=services.actor(authority), idempotency_key='space')
    background = _BusinessBackgroundService(services, authority)
    runs = WorkflowRunStore(home.owner_home_dir)
    submissions, completions, reviews, effects, resolutions, performances = [], [], [], [], [], []
    completion_changes = {}
    submit, perform = background.submit, WorkflowEngine._perform

    def observe_submit(request, *, actor, idempotency_key):
        submissions.append({'request': dict(request), 'actor': actor, 'key': idempotency_key})
        return submit(request, actor=actor, idempotency_key=idempotency_key)

    def observe_perform(self, record, spec, *args, **kwargs):
        performances.append(spec['step_id'])
        return perform(self, record, spec, *args, **kwargs)

    def completion(task_id, **identity):
        completions.append({'task_id': task_id, **identity})
        task = store.session_tasks.require(task_id)
        if task.status != 'done':
            return None
        return {**identity, 'task_id': task.task_id, 'status': 'completed',
            'completion_ref': 'canonical-completion', 'output': {'value': 7},
            'run_id': task.task_run_id, 'result_ref': task.result_refs[0], **completion_changes}

    def review(ref, record, step_id):
        target = next(item for item in record.steps if item.step_id == step_id)
        reviews.append((record.workflow_run_id, step_id, target.escalation_result['attempt_id']))
        return {'receipt_ref': ref, 'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context['task_id'], 'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(runs.load_workflow_snapshot(record.workflow_run_id)),
            'author_id': 'writer', 'reviewer_id': 'independent-reviewer', 'decision': 'approved',
            'expires_at': 9999999999}

    def effect(record, step):
        effects.append((record.workflow_run_id, step.step_id))
        return None

    def resolution(ref, record, step):
        resolutions.append(ref)
        return {'resolution_ref': ref, 'state': 'committed', 'output': {'value': 'not-canonical'},
            'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context['task_id'], 'step_id': step.step_id,
            'attempt_id': step.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(runs.load_workflow_snapshot(record.workflow_run_id))}

    guard = WorkflowGovernance(runs, policy_provider=lambda record, phase: {
        'author_id': 'writer', 'review_refs': ['review-1'], 'minimum_reviews': 1},
        receipt_reader=review, authorization_check=lambda receipt, record: access['review_allowed'],
        effect_reader=effect, resolution_reader=resolution) if request.param else None
    monkeypatch.setattr(background, 'submit', observe_submit)
    monkeypatch.setattr(WorkflowEngine, '_perform', observe_perform)
    monkeypatch.setattr(ScriptRunner, 'run_script', lambda *a, **k: pytest.fail('single-agent definition must not run scripts'))
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor, completion_reader=completion)
    context = BusinessExecutionContext('alice', space['space_id'], None, 'stask-parent',
        space['revision'], None, (), (), 'business-context.v1')
    api = WorkflowScopeService(home.owner_home_dir, owner_id='alice',
        authorize=lambda current, action, ref: current is actor and access['allowed'] and action not in access['denied'],
        context_provider=lambda current, params: context, agent_steps=agents, governance=guard,
        engine_factory=WorkflowEngine)
    definition = {'descriptor': {'id': 'original-flow', 'kind': 'workflow', 'name': '原任务恢复',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'agent', 'name': '原 Agent 步骤', 'mode': 'agent_task',
            'config': {'instruction': '核对原输入'}}]}
    api.execute('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space['space_id']]}}, actor=actor, idempotency_key='create')
    return SimpleNamespace(home=home, store=store, actor=actor, access=access, api=api, background=background,
        runs=runs, agents=agents, guard=guard, space=space, context=context, submissions=submissions,
        completions=completions, reviews=reviews, effects=effects, resolutions=resolutions,
        performances=performances, completion_changes=completion_changes)


def start(fixture):
    return fixture.api.execute('workflows.run', {'workflow_ref': 'original-flow',
        'space_id': fixture.space['space_id']}, actor=fixture.actor, idempotency_key='run')


def resume(fixture, original, key, **request):
    current = fixture.runs.load_run(original.workflow_run_id)
    receipt = fixture.api.execute('workflow_runs.resume', {'workflow_run_id': original.workflow_run_id, **request},
        actor=fixture.actor, expected_revision=current.revision, idempotency_key=key)
    return receipt, fixture.runs.load_run(original.workflow_run_id)


def tasks(fixture):
    rows, errors = fixture.store.session_tasks.list_report(limit=0)
    assert errors == []
    return rows


def complete(fixture, task_id):
    for status in ('accepted', 'done'):
        fixture.store.session_tasks.advance(task_id, SessionTaskUpdate(status=status,
            task_run_id='canonical-agent-run', result_refs=('canonical-result',)))


def paused_after_submission(fixture, monkeypatch):
    """只在真实任务提交成功后的 waiting 步骤写叶注入一次 EIO，其余持久化不变。"""
    write, failures = run_records._write_json, []

    def fail(root, parts, value):
        data = payload(value)
        if parts[-2:] == ('steps', 'agent.json') and data['status'] == 'waiting_for_agent' and not failures:
            task = fixture.store.session_tasks.require(data['escalation_result']['task_id'])
            assert task.status == 'queued' and len(fixture.submissions) == 1
            failures.append(data)
            raise OSError(errno.EIO, 'one waiting step snapshot EIO after canonical submission')
        return write(root, parts, value)

    monkeypatch.setattr(run_records, '_write_json', fail)
    with pytest.raises(WorkflowExecutionError) as caught:
        start(fixture)
    first = fixture.runs.load_run(caught.value.record.workflow_run_id)
    assert len(failures) == 1 and first.status == 'paused_for_resume' and first.effect_outcome == 'unknown'
    assert caught.value.receipt['effect_outcome'] == 'unknown'
    assert first.steps[0].status == 'waiting_for_agent' and first.steps[0].output_snapshot == {}
    assert first.steps[0].escalation_result['submission_state'] == 'accepted'
    assert len(tasks(fixture)) == 1 and len(fixture.store.wakes.pending(limit=0)) == 1
    assert fixture.completions == [] and fixture.performances == ['agent']
    if fixture.guard is not None:
        assert len(fixture.reviews) == 1
    return first


def assert_same_identity(fixture, first, current):
    assert (current.workflow_run_id, current.workflow_id, current.owner_id) == (
        first.workflow_run_id, first.workflow_id, first.owner_id)
    assert current.business_context == first.business_context and current.scope_snapshot == first.scope_snapshot
    assert current.scope_revision == first.scope_revision and current.execution_definition_ref == first.execution_definition_ref
    before, after = first.steps[0], current.steps[0]
    assert (after.step_id, after.started_at) == (before.step_id, before.started_at)
    for key in ('attempt_id', 'submission_key', 'task_id'):
        assert after.escalation_result.get(key) == before.escalation_result.get(key)
    assert fixture.performances == ['agent']


def assert_finished(fixture, first, receipt, done):
    assert receipt['state'] == 'completed' and done.status == 'succeeded' and done.effect_outcome == 'confirmed'
    assert done.steps[0].status == 'succeeded' and done.steps[0].output_snapshot == {'value': 7}
    assert done.steps[0].escalation_result['completion_ref'] == 'canonical-completion'
    assert done.steps[0].escalation_result['run_id'] == 'canonical-agent-run'
    assert done.steps[0].escalation_result['result_ref'] == 'canonical-result'
    assert_same_identity(fixture, first, done)
    task = tasks(fixture)[0]
    assert len(tasks(fixture)) == 1 and task.task_id == first.steps[0].escalation_result['task_id']
    assert task.status == 'done' and fixture.effects == []
    assert fixture.completions and all(call == {'task_id': task.task_id, 'owner_id': first.owner_id,
        'workflow_run_id': first.workflow_run_id, 'step_id': 'agent',
        'attempt_id': first.steps[0].escalation_result['attempt_id']} for call in fixture.completions)


@pytest.mark.parametrize('already_done', [False, True], ids=['pending-first', 'done-first'])
def test_public_snapshot_eio_reads_original_completion_without_resubmission(
        public_recovery, monkeypatch, already_done):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    task_id = first.steps[0].escalation_result['task_id']
    review_count = len(f.reviews)
    if not already_done:
        receipt, waiting = resume(f, first, 'pending-resume')
        assert receipt['state'] == 'pending' and waiting.status == 'waiting_for_agent'
        assert waiting.effect_outcome == 'unknown' and waiting.steps[0].output_snapshot == {}
        assert not waiting.steps[0].escalation_result.get('completion_ref')
        assert len(f.completions) == 1 and len(f.submissions) == 1
        assert_same_identity(f, first, waiting)
    complete(f, task_id)
    receipt, done = resume(f, first, 'completion-resume')
    assert_finished(f, first, receipt, done)
    assert len(f.submissions) == 1 and len(f.completions) == (1 if already_done else 2)
    assert len(f.reviews) == review_count  # 读取旧完成链不是新副作用，不消费新审核。
    before = len(f.completions)
    _, again = resume(f, first, 'completed-again')
    assert again.status == 'succeeded' and len(f.completions) == before and len(f.submissions) == 1


def test_public_snapshot_recovery_preserves_control_cas_and_replay(public_recovery, monkeypatch):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    receipt, waiting = resume(f, first, 'same-control')
    calls = list(f.completions)
    replay = f.api.execute('workflow_runs.resume', {'workflow_run_id': first.workflow_run_id}, actor=f.actor,
        expected_revision=first.revision, idempotency_key='same-control')
    assert replay == receipt and f.completions == calls
    assert f.runs.load_run(first.workflow_run_id).to_dict() == waiting.to_dict()
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        f.api.execute('workflow_runs.resume', {'workflow_run_id': first.workflow_run_id}, actor=f.actor,
            expected_revision=first.revision, idempotency_key='stale-new-control')
    assert f.completions == calls and len(f.submissions) == 1
    # 等待期间锁已释放；持有原锁时另一恢复入口仍必须拒绝。
    with f.runs.execution_lock(first.workflow_run_id), pytest.raises(RuntimeError, match='正在执行'):
        resume(f, first, 'nested-control')
    complete(f, first.steps[0].escalation_result['task_id'])
    receipt, done = resume(f, first, 'finish')
    assert_finished(f, first, receipt, done)


@pytest.mark.parametrize('denied', ['workflow_runs.resume', 'workflows.run', 'foreign_actor'])
def test_public_native_recovery_rechecks_current_authorization(public_recovery, monkeypatch, denied):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    complete(f, first.steps[0].escalation_result['task_id'])
    actor = replace(f.actor, owner_id='bob') if denied == 'foreign_actor' else f.actor
    f.access['denied'].add(denied)
    with pytest.raises(PermissionError, match='ACCESS_DENIED'):
        f.api.execute('workflow_runs.resume', {'workflow_run_id': first.workflow_run_id}, actor=actor,
            expected_revision=first.revision, idempotency_key='revoked')
    assert f.completions == [] and len(f.submissions) == 1
    assert f.runs.load_run(first.workflow_run_id).to_dict() == first.to_dict()


def test_legacy_resume_cannot_bypass_business_authorization(public_recovery, monkeypatch):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    complete(f, first.steps[0].escalation_result['task_id'])
    tool = WorkflowsTool(f.home.owner_home_dir, engine=WorkflowEngine(f.home.owner_home_dir, agent_steps=f.agents))
    result = tool.execute({'action': 'resume', 'workflow_run_id': first.workflow_run_id})
    assert not result.ok and result.error_code == 'TOOL_PERMISSION_DENIED'
    assert 'BUSINESS_AUTHORIZATION_UNAVAILABLE' in result.output
    f.access['denied'].add('workflows.run')
    tool = WorkflowsTool(f.home.owner_home_dir, engine=f.api._engine(f.actor))
    result = tool.execute({'action': 'resume', 'workflow_run_id': first.workflow_run_id})
    assert not result.ok and result.error_code == 'TOOL_PERMISSION_DENIED'
    assert f.completions == [] and len(f.submissions) == 1


@pytest.mark.parametrize('effect_outcome', ['unknown', 'failed'])
def test_generic_unknown_script_effect_still_requires_reconciliation(public_recovery, effect_outcome):
    f = public_recovery
    definition = {'descriptor': {'id': 'script-flow', 'kind': 'workflow', 'name': '普通脚本',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'script', 'name': '脚本', 'mode': 'deterministic_script', 'script_id': 'never-run'}]}
    f.api.execute('workflows.create', {'definition': definition}, actor=f.actor, idempotency_key='script-create')
    definition = f.api.store.get_workflow('script-flow')
    # 只重建原有未知脚本记录，不执行脚本；恢复门必须在任何执行前拒绝。
    step = StepExecutionRecord('script', '脚本', 'deterministic_script', 'paused_for_resume', 1, 2, 1, False,
        script_id='never-run', needs_repair=True, escalation_result={'attempt_id': 'original-script-attempt', 'effect_started': True})
    original = WorkflowRunRecord('unknown-script', 'script-flow', 'alice', 'manual', 'paused_for_resume', 1, 2, 1,
        steps=(step,), current_step_id='script', needs_repair=True, business_context=f.context.to_dict(),
        scope_snapshot={'scope_mode': 'owner_global', 'space_ids': [], 'scope_revision': 1}, scope_revision=1)
    execution_receipt(original, phase='started', effect_outcome=effect_outcome)
    f.runs.create_run(original, definition.to_dict())
    with pytest.raises(PermissionError, match='RECONCILIATION_REQUIRED'):
        resume(f, original, 'script-resume')
    current = f.runs.load_run(original.workflow_run_id)
    assert current.effect_outcome == effect_outcome and current.steps == original.steps
    assert f.submissions == [] and f.completions == [] and f.performances == []
    assert len(f.effects) == (1 if f.guard is not None else 0)


def test_client_agent_output_and_generic_resolution_cannot_manufacture_completion(public_recovery, monkeypatch):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    with pytest.raises(ValueError, match='INVALID_WORKFLOW_PARAMETERS'):
        resume(f, first, 'client-patch', patched_step_output={'value': 'client-completion'})
    with pytest.raises(ValueError, match='AGENT_COMPLETION_HOST_ONLY'):
        f.api._engine(f.actor).resume_workflow_run(first.workflow_run_id,
            patched_step_output={'value': 'client-completion'}, resolution_origin='system')
    error = ValueError if f.guard is not None else PermissionError
    code = 'AGENT_COMPLETION_HOST_ONLY' if f.guard is not None else 'RESOLUTION_UNAVAILABLE'
    with pytest.raises(error, match=code):
        resume(f, first, 'generic-resolution', resolution_ref='resolution-1')
    current = f.runs.load_run(first.workflow_run_id)
    assert current.effect_outcome == 'unknown' and current.steps == first.steps
    assert f.completions == [] and len(f.submissions) == 1
    complete(f, first.steps[0].escalation_result['task_id'])
    receipt, done = resume(f, first, 'canonical-only')
    assert_finished(f, first, receipt, done)


@pytest.mark.parametrize('changes', [{'task_id': 'different-task'}, {'attempt_id': 'different-attempt'},
    {'completion_ref': ''}, {'output': 'client-text'}, {'status': 'untrusted-success'}])
def test_untrusted_completion_cannot_commit_original_agent_output(public_recovery, monkeypatch, changes):
    f = public_recovery
    first = paused_after_submission(f, monkeypatch)
    complete(f, first.steps[0].escalation_result['task_id'])
    f.completion_changes.update(changes)
    with pytest.raises(ValueError, match='WORKFLOW_AGENT_'):
        resume(f, first, 'untrusted-completion')
    current = f.runs.load_run(first.workflow_run_id)
    assert current.effect_outcome == 'unknown' and current.steps[0].output_snapshot == {}
    assert not current.steps[0].escalation_result.get('completion_ref')
    assert len(f.completions) == 1 and len(f.submissions) == 1
    f.completion_changes.clear()
    receipt, done = resume(f, first, 'trusted-completion')
    assert_finished(f, first, receipt, done)


def test_absence_proof_and_later_resubmission_keep_original_identity_and_fresh_review(public_recovery, monkeypatch):
    f = public_recovery
    write, failures = task_records.write_json_file_atomic, []

    def fail(path, data, *args, **kwargs):
        if data.get('schema_version') == 'session_task.v1' and not failures:
            failures.append(dict(data))
            raise OSError(errno.EIO, 'one pre-publication canonical task EIO')
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(task_records, 'write_json_file_atomic', fail)
    receipt = start(f)
    first = f.runs.load_run(receipt['workflow_run_id'])
    assert first.status == 'waiting_for_agent' and len(failures) == 1 and tasks(f) == []
    original = first.steps[0].escalation_result
    reserved = failures[0]['task_id']
    _, absent = resume(f, first, 'prove-absence')
    assert absent.steps[0].escalation_result['reconciliation_state'] == 'absent'
    assert absent.steps[0].escalation_result['submission_state'] == 'not_started'
    assert absent.steps[0].escalation_result['session_task_dispatch'] == original['session_task_dispatch']
    assert tasks(f) == [] and f.completions == [] and len(f.submissions) == 1
    assert_same_identity(f, first, absent)
    if f.guard is not None:
        f.access['review_allowed'] = False
        with pytest.raises(PermissionError, match='REVIEW_NOT_CURRENT'):
            resume(f, first, 'denied-fresh-review')
        assert tasks(f) == [] and len(f.submissions) == 1 and f.completions == []
        f.access['review_allowed'] = True
    _, retried = resume(f, first, 'explicit-resubmit')
    task = tasks(f)[0]
    result = retried.steps[0].escalation_result
    assert task.task_id == reserved == result['task_id'] and len(tasks(f)) == 1
    assert task.dedupe_key == original['session_task_dispatch']['dedupe_key']
    assert task.sender_thread_id == original['session_task_dispatch']['sender_thread_id']
    assert task.target_thread_id == original['session_task_dispatch']['target_thread_id']
    assert result['attempt_id'] == original['attempt_id'] and result['submission_key'] == original['submission_key']
    assert len(f.submissions) == 2 and f.submissions[0]['key'] == f.submissions[1]['key']
    assert f.completions == [] and f.performances == ['agent']
    complete(f, reserved)
    receipt, done = resume(f, first, 'finish-original')
    # 原来尚无 task_id；只允许补上 canonical 预留 ID，其余身份不变。
    first.steps[0].escalation_result['task_id'] = reserved
    assert_finished(f, first, receipt, done)
    assert len(f.submissions) == 2
