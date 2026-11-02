"""P1-R4-03：公开工作流动作经真实适配器恢复原预留任务，不替换执行端口。"""
from __future__ import annotations

import errno
import json
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
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows import engine as engine_module
from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_workflow_script_engine import script, step


# LLM: 所有业务层和存储真实运行；仅观察submit/脚本调用及注入明确IO故障，模型不得运行。
# 函数用途: 从公开create/run/get/resume入口构造同一个冻结工作流。
@pytest.fixture
def public_workflow(tmp_path, monkeypatch):
    assert Path(engine_module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    home = replace(home_paths(tmp_path / 'home'), owner_id='alice', owner_provider='local', owner_kind='main')
    store = ConversationStore(home.owner_home_dir / 'conversations')
    source = store.threads.create(owner_id=home.owner_id, canonical_user_id='original-user')
    actor = HostChannelActor(home.owner_id, source.thread_id, 'original-entry', 'local', 'local', 'user')
    access = {'allowed': True}
    host = SimpleNamespace(home_paths=home, conversation_store=store,
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'),
        run=lambda *a, **k: pytest.fail('absence recovery must never run a model'))
    services = BusinessServices(host)
    authority = bind_business_context(host, actor=actor, authorize=lambda action, facts: access['allowed'])
    space = services.spaces.create({'name': '原空间'}, actor=services.actor(authority), idempotency_key='original-space')
    background = _BusinessBackgroundService(services, authority)
    submissions, scripts, completions, engines = [], [], [], []
    submit, run_script = background.submit, ScriptRunner.run_script

    def observe_submit(request, *, actor, idempotency_key):
        submissions.append({'request': dict(request), 'actor': actor, 'key': idempotency_key})
        return submit(request, actor=actor, idempotency_key=idempotency_key)

    def observe_script(self, script_id, *args, **kwargs):
        scripts.append(script_id)
        return run_script(self, script_id, *args, **kwargs)

    def completion(task_id, **identity):
        completions.append(task_id)
        task = store.session_tasks.require(task_id)
        if task.status != 'done':
            return None
        return {**identity, 'task_id': task_id, 'status': 'completed', 'completion_ref': 'canonical-completion',
            'output': {'value': 7}, 'run_id': task.task_run_id, 'result_ref': task.result_refs[0]}

    def engine_factory(owner_home, **kwargs):
        engine = WorkflowEngine(owner_home, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(background, 'submit', observe_submit)
    monkeypatch.setattr(ScriptRunner, 'run_script', observe_script)
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor, completion_reader=completion)
    context = BusinessExecutionContext(home.owner_id, space['space_id'], None, 'stask-parent',
        space['revision'], None, (), (), 'business-context.v1')
    api = WorkflowScopeService(home.owner_home_dir, owner_id=home.owner_id,
        authorize=lambda current, action, ref: current is actor and access['allowed'],
        context_provider=lambda current, request: context, agent_steps=agents, engine_factory=engine_factory)
    script(home.owner_home_dir, script_id='prefix')
    script(home.owner_home_dir, script_id='tail')
    definition = {'descriptor': {'id': 'original-flow', 'kind': 'workflow', 'name': '原工作流',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [step('prefix', script_id='prefix'), step('agent', mode='agent_task', script_id='',
            config={'instruction': '核对原输入'}), step('tail', script_id='tail')]}
    api.execute('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space['space_id']]}},
        actor=actor, idempotency_key='create-original')
    return SimpleNamespace(home=home, store=store, actor=actor, access=access, api=api, background=background,
        agents=agents, runs=WorkflowRunStore(home.owner_home_dir), submissions=submissions,
        scripts=scripts, completions=completions, engines=engines, space=space)


def start(fixture):
    receipt = fixture.api.execute('workflows.run', {'workflow_ref': 'original-flow', 'space_id': fixture.space['space_id']},
        actor=fixture.actor, idempotency_key='run-original')
    assert receipt['state'] == 'pending'
    return fixture.runs.load_run(receipt['workflow_run_id'])


def resume(fixture, original, key):
    current = fixture.api.execute('workflow_runs.get', {'workflow_run_id': original.workflow_run_id}, actor=fixture.actor)
    request = {'workflow_run_id': original.workflow_run_id}
    receipt = fixture.api.execute('workflow_runs.resume', request, actor=fixture.actor,
        expected_revision=current['run']['revision'], idempotency_key=key)
    return receipt, fixture.runs.load_run(original.workflow_run_id)


def tasks(fixture):
    rows, errors = fixture.store.session_tasks.list_report(limit=0)
    assert errors == []
    return rows


# LLM: 在session_task.v1首个canonical发表前抛一次EIO；真实预留文件及其原task_id保持不变。
# 函数用途: 重现已预留ID但尚无任务可等待的原始故障。
def fail_publication_once(monkeypatch):
    write, failures = task_records.write_json_file_atomic, []

    def fail(path, data, *args, **kwargs):
        if data.get('schema_version') == 'session_task.v1' and not failures:
            failures.append(dict(data))
            raise OSError(errno.EIO, 'one pre-publication canonical SessionTask EIO')
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(task_records, 'write_json_file_atomic', fail)
    return failures


def assert_same_attempt(original, current):
    assert current.workflow_run_id == original.workflow_run_id
    assert current.business_context == original.business_context and current.scope_snapshot == original.scope_snapshot
    assert current.scope_revision == original.scope_revision
    assert current.steps[0] == original.steps[0] and current.steps[2] == original.steps[2]
    before, after = original.steps[1], current.steps[1]
    assert (after.step_id, after.started_at) == (before.step_id, before.started_at)
    assert after.escalation_result['attempt_id'] == before.escalation_result['attempt_id']
    assert after.escalation_result['submission_key'] == before.escalation_result['submission_key']


def complete(fixture, original, task_id):
    for status in ('accepted', 'done'):
        fixture.store.session_tasks.advance(task_id, SessionTaskUpdate(status=status,
            task_run_id='canonical-agent-run', result_refs=('canonical-result',)))
    receipt, done = resume(fixture, original, 'resume-completion')
    assert receipt['state'] == 'completed' and done.status == 'succeeded'
    assert done.steps[0] == original.steps[0]
    assert done.steps[1].escalation_result['task_id'] == task_id
    assert done.steps[1].escalation_result['attempt_id'] == original.steps[1].escalation_result['attempt_id']
    assert done.steps[1].output_snapshot == {'value': 7}
    assert fixture.scripts == ['prefix', 'tail']
    assert len(tasks(fixture)) == 1
    return done


def test_public_healthy_control_creates_one_original_task(public_workflow):
    fixture = public_workflow
    first = start(fixture)
    assert first.status == 'waiting_for_agent'
    task = tasks(fixture)[0]
    assert len(fixture.submissions) == 1 and fixture.scripts == ['prefix']
    assert task.task_id == first.steps[1].escalation_result['task_id']
    assert len(fixture.store.wakes.pending(limit=0)) == 1
    complete(fixture, first, task.task_id)
    assert len(fixture.submissions) == 1


def test_public_prepublication_eio_later_resume_reuses_reserved_task(public_workflow, monkeypatch, record_property):
    fixture = public_workflow
    failures = fail_publication_once(monkeypatch)
    first = start(fixture)
    assert len(failures) == 1 and first.status == 'waiting_for_agent'
    original = first.steps[1].escalation_result
    assert original['submission_state'] == 'unknown' and not original.get('task_id')
    assert tasks(fixture) == [] and fixture.store.wakes.pending(limit=0) == []
    reservation = json.loads(fixture.store.session_tasks._dedupe_path(original['session_task_dispatch']['dedupe_key']).read_text())
    assert reservation['task_id'] == failures[0]['task_id'] and reservation['intent_fingerprint']
    first_receipt, absent = resume(fixture, first, 'reconcile-only')
    result = absent.steps[1].escalation_result
    assert result['submission_state'] == 'not_started' and result['reconciliation_state'] == 'absent'
    assert result['session_task_dispatch'] == original['session_task_dispatch']
    assert first_receipt['state'] == 'pending' and tasks(fixture) == []
    assert len(fixture.submissions) == 1 and fixture.scripts == ['prefix'] and fixture.completions == []
    assert_same_attempt(first, absent)
    receipt, retried = resume(fixture, first, 'retry-original')
    assert receipt['state'] == 'pending'
    rows = tasks(fixture)
    assert len(rows) == 1, 'a later explicit resume must submit the proven-absent original task'
    task = rows[0]
    assert task.task_id == reservation['task_id']
    assert task.dedupe_key == original['session_task_dispatch']['dedupe_key']
    assert task.sender_thread_id == original['session_task_dispatch']['sender_thread_id']
    assert task.target_thread_id == original['session_task_dispatch']['target_thread_id']
    assert task.metadata['business_context']['space_id'] == first.business_context['space_id']
    assert len(fixture.submissions) == 2 and fixture.submissions[0]['key'] == fixture.submissions[1]['key']
    assert fixture.scripts == ['prefix'] and len(fixture.store.wakes.pending(limit=0)) == 1
    assert_same_attempt(first, retried)
    done = complete(fixture, first, task.task_id)
    assert len(fixture.submissions) == 2
    record_property('absence_resume_evidence', json.dumps({'reserved_task_id': reservation['task_id'],
        'final_task_id': task.task_id, 'canonical_tasks': len(tasks(fixture)), 'submissions': len(fixture.submissions),
        'workflow_status': done.status, 'scripts': fixture.scripts, 'first_recovery_tasks': 0}))


def proven_absence(fixture, monkeypatch):
    failures = fail_publication_once(monkeypatch)
    first = start(fixture)
    _, absent = resume(fixture, first, 'reconcile-only')
    assert failures and tasks(fixture) == []
    assert absent.steps[1].escalation_result['submission_state'] == 'not_started'
    assert absent.steps[1].escalation_result['reconciliation_state'] == 'absent'
    assert fixture.completions == [] and len(fixture.submissions) == 1
    return first, absent, failures[0]['task_id']


def publish_original(fixture, first):
    original = fixture.submissions[0]
    return fixture.background.submit(original['request'], actor=original['actor'],
        idempotency_key=first.steps[1].escalation_result['submission_key'])


def test_public_resume_replays_original_controls_and_rejects_stale_revision(public_workflow, monkeypatch):
    fixture = public_workflow
    first, absent, reserved = proven_absence(fixture, monkeypatch)
    request = {'workflow_run_id': first.workflow_run_id}
    replay = fixture.api.execute('workflow_runs.resume', request, actor=fixture.actor,
        expected_revision=first.revision, idempotency_key='reconcile-only')
    assert replay['run']['revision'] == absent.revision
    assert fixture.runs.load_run(first.workflow_run_id).to_dict() == absent.to_dict()
    assert tasks(fixture) == [] and len(fixture.submissions) == 1
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        fixture.api.execute('workflow_runs.resume', request, actor=fixture.actor,
            expected_revision=first.revision, idempotency_key='stale-fresh-key')
    receipt, retried = resume(fixture, first, 'retry-original')
    assert [item.task_id for item in tasks(fixture)] == [reserved]
    for key, expected in [('retry-original', absent.revision), ('reconcile-only', first.revision)]:
        fixture.api.execute('workflow_runs.resume', request, actor=fixture.actor,
            expected_revision=expected, idempotency_key=key)
    assert fixture.runs.load_run(first.workflow_run_id).to_dict() == retried.to_dict()
    assert receipt['state'] == 'pending' and len(fixture.submissions) == 2
    assert fixture.scripts == ['prefix']


@pytest.mark.parametrize('window', ['before_resume', 'after_admission'])
def test_public_resume_binds_concurrently_appearing_original_task(public_workflow, monkeypatch, window):
    fixture = public_workflow
    first, _, reserved = proven_absence(fixture, monkeypatch)
    appeared = []
    if window == 'before_resume':
        appeared.append(publish_original(fixture, first))
    else:
        admit = WorkflowEngine._admit_effect

        def publish_after_admission(self, run, record):
            admit(self, run, record)
            if record.step_id == 'agent' and not appeared:
                appeared.append(publish_original(fixture, first))

        monkeypatch.setattr(WorkflowEngine, '_admit_effect', publish_after_admission)
    _, recovered = resume(fixture, first, 'resume-with-appearance')
    assert appeared and recovered.steps[1].escalation_result['task_id'] == reserved
    assert [item.task_id for item in tasks(fixture)] == [reserved]
    assert len(fixture.submissions) == 2  # 原失败提交＋竞争者；恢复不能第三次调用submit。
    assert fixture.completions == [] and fixture.scripts == ['prefix']
    assert len(fixture.store.wakes.pending(limit=0)) == 1
    assert_same_attempt(first, recovered)


@pytest.mark.parametrize('window', ['before_resume', 'after_absence_read'])
def test_public_absent_retry_rechecks_current_authorization(public_workflow, monkeypatch, window):
    fixture = public_workflow
    first, _, reserved = proven_absence(fixture, monkeypatch)
    if window == 'before_resume':
        fixture.access['allowed'] = False
    else:
        page = fixture.store.session_tasks.list_page

        def revoke_after_read(**kwargs):
            result = page(**kwargs)
            fixture.access['allowed'] = False
            return result

        monkeypatch.setattr(fixture.store.session_tasks, 'list_page', revoke_after_read)
    with pytest.raises(PermissionError):
        resume(fixture, first, 'revoked-retry')
    assert tasks(fixture) == [] and len(fixture.submissions) == 1
    assert fixture.store.wakes.pending(limit=0) == [] and fixture.completions == []
    assert fixture.scripts == ['prefix']
    assert fixture.store.session_tasks.load(reserved) is None


@pytest.mark.parametrize('failure', ['partial', 'io', 'load_error'])
def test_public_absent_retry_requires_fresh_complete_storage_evidence(public_workflow, monkeypatch, failure):
    fixture = public_workflow
    first, _, reserved = proven_absence(fixture, monkeypatch)
    page = fixture.store.session_tasks.list_page

    def uncertain(**kwargs):
        if failure == 'io':
            raise OSError(errno.EIO, 'canonical read unavailable')
        return {'items': [], 'next_cursor': None, 'coverage': 'partial' if failure == 'partial' else 'complete',
            'load_errors': [{'code': 'CANONICAL_UNREADABLE'}] if failure == 'load_error' else []}

    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', uncertain)
    _, waiting = resume(fixture, first, 'uncertain-retry')
    assert waiting.steps[1].escalation_result['reconciliation_state'] == 'unknown'
    assert tasks(fixture) == [] and len(fixture.submissions) == 1
    assert fixture.completions == [] and fixture.scripts == ['prefix']
    monkeypatch.setattr(fixture.store.session_tasks, 'list_page', page)
    _, checked = resume(fixture, first, 'reconcile-again')
    assert checked.steps[1].escalation_result['reconciliation_state'] == 'absent'
    assert tasks(fixture) == [] and len(fixture.submissions) == 1
    _, retried = resume(fixture, first, 'retry-after-new-proof')
    assert retried.steps[1].escalation_result['task_id'] == reserved
    assert len(tasks(fixture)) == 1 and len(fixture.submissions) == 2


def test_public_absent_retry_with_missing_identity_stays_closed(public_workflow, monkeypatch):
    fixture = public_workflow
    first, absent, _ = proven_absence(fixture, monkeypatch)
    absent.steps[1].escalation_result['session_task_dispatch'] = {}
    fixture.runs.save_run(absent)
    _, waiting = resume(fixture, first, 'missing-original-identity')
    assert waiting.status == 'waiting_for_agent'
    assert tasks(fixture) == [] and len(fixture.submissions) == 1 and fixture.completions == []
    assert fixture.scripts == ['prefix'] and fixture.store.wakes.pending(limit=0) == []


@pytest.mark.parametrize('field', ['actor_source', 'sender_thread_id', 'target_thread_id', 'dedupe_key', 'owner'])
def test_public_absent_retry_rejects_changed_original_binding(public_workflow, monkeypatch, field):
    fixture = public_workflow
    first, absent, _ = proven_absence(fixture, monkeypatch)
    other = fixture.store.threads.create(owner_id='alice', canonical_user_id='different-source')
    if field in ('actor_source', 'owner'):
        changed = replace(fixture.actor, **({'source_thread_id': other.thread_id} if field == 'actor_source' else {'owner_id': 'bob'}))
        fixture.agents.actor_provider = lambda run: changed
    else:
        absent.steps[1].escalation_result['session_task_dispatch'][field] = 'different-key' if field == 'dedupe_key' else other.thread_id
        fixture.runs.save_run(absent)
    with pytest.raises(PermissionError):
        resume(fixture, first, 'changed-original-binding')
    assert tasks(fixture) == [] and len(fixture.submissions) == 1
    assert fixture.scripts == ['prefix'] and fixture.completions == []
    assert fixture.store.wakes.pending(limit=0) == []


def test_public_lost_create_ack_reconciles_without_duplicate_submission(public_workflow, monkeypatch):
    fixture = public_workflow
    create, faults = fixture.store.session_tasks.create, []

    def lose_one_ack(*args, **kwargs):
        result = create(*args, **kwargs)
        if not faults:
            faults.append(result.task_id)
            raise OSError(errno.EIO, 'canonical task exists but creation ACK was lost')
        return result

    monkeypatch.setattr(fixture.store.session_tasks, 'create', lose_one_ack)
    first = start(fixture)
    assert tasks(fixture)[0].task_id == faults[0]
    assert first.steps[1].escalation_result['submission_state'] == 'unknown'
    for index in range(3):
        _, recovered = resume(fixture, first, f'explicit-reconcile-{index}')
        assert recovered.steps[1].escalation_result['task_id'] == faults[0]
        assert_same_attempt(first, recovered)
    assert len(tasks(fixture)) == 1 and len(fixture.submissions) == 1
    assert fixture.scripts == ['prefix'] and fixture.store.wakes.pending(limit=0) == []
