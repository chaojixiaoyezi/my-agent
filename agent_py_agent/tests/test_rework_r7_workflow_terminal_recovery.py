"""P1-R7-01：终态 run.json 单次发表 EIO 后，公开恢复只补发原运行终态，不重放已确认工作。"""
from __future__ import annotations

import errno
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    replace_run_record,
)
from agent_py_agent.agent.workflows.script_runner import ScriptRunner

TERMINAL_BREAKPOINT = {'error_code': 'WORKFLOW_EXECUTION_FAILED', 'phase': 'terminal', 'can_resume': True}


def _workflow(workflow_id, contract=None):
    return {'descriptor': {'id': workflow_id, 'kind': 'workflow', 'name': '终态发表恢复',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': contract or {}},
        'steps': [{'step_id': 'agent', 'name': '原 Agent 步骤', 'mode': 'agent_task',
            'config': {'instruction': '核对原输入'}}]}


# LLM: 在同一home上装配真实领域服务、双Store、治理门与同一发行上下文签发的BusinessActor，支持重复调用模拟进程重启。
# 函数用途: 为终态发表恢复测试装配或重装配工作流领域服务与宿主后台适配器。
def _wire(f):
    """真实领域服务/引擎/双 Store/适配器；同一 home 再次装配即模拟进程重启。"""
    store = ConversationStore(f.home.owner_home_dir / 'conversations')
    host = SimpleNamespace(home_paths=f.home, conversation_store=store,
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'),
        run=lambda *a, **k: pytest.fail('terminal recovery must not run a model'))
    services = BusinessServices(host)
    authority = bind_business_context(host, actor=f.host_actor, authorize=lambda action, facts: f.access['allowed'])
    background = _BusinessBackgroundService(services, authority)
    runs = WorkflowRunStore(f.home.owner_home_dir)
    submit = background.submit

    def observe_submit(request, *, actor, idempotency_key):
        f.submissions.append(idempotency_key)
        return submit(request, actor=actor, idempotency_key=idempotency_key)

    def completion(task_id, **identity):
        f.completions.append({'task_id': task_id, **identity})
        task = store.session_tasks.require(task_id)
        if task.status != 'done':
            return None
        return {**identity, 'task_id': task.task_id, 'status': 'completed',
            'completion_ref': 'canonical-completion', 'output': {'value': 7},
            'run_id': task.task_run_id, 'result_ref': task.result_refs[0]}

    def review(ref, record, step_id):
        target = next(item for item in record.steps if item.step_id == step_id)
        f.reviews.append((record.workflow_run_id, step_id, target.escalation_result['attempt_id']))
        return {'receipt_ref': ref, 'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context['task_id'], 'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(runs.load_workflow_snapshot(record.workflow_run_id)),
            'author_id': 'writer', 'reviewer_id': 'independent-reviewer', 'decision': 'approved',
            'expires_at': 9999999999}

    def effect(record, step):
        f.effects.append((record.workflow_run_id, step.step_id))
        return None

    def resolution(ref, record, step):
        f.resolutions.append(ref)
        return None if f.resolution_state is None else {'resolution_ref': ref, 'state': f.resolution_state,
            'output': {'value': 'not-canonical'}, 'owner_id': record.owner_id,
            'workflow_run_id': record.workflow_run_id, 'task_id': record.business_context['task_id'],
            'step_id': step.step_id, 'attempt_id': step.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(runs.load_workflow_snapshot(record.workflow_run_id))}

    background.submit = observe_submit
    guard = WorkflowGovernance(runs, policy_provider=lambda record, phase: {
        'author_id': 'writer', 'review_refs': ['review-1'], 'minimum_reviews': 1},
        receipt_reader=review, authorization_check=lambda receipt, record: f.access['review_allowed'],
        effect_reader=effect, resolution_reader=resolution) if f.governed else None
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: f.host_actor, completion_reader=completion)
    api = WorkflowScopeService(f.home.owner_home_dir, owner_id='alice',
        authorize=lambda current, action, ref: (current is f.actor and f.access['allowed']
                                                and action not in f.access['denied']),
        context_provider=lambda current, params: f.context, agent_steps=agents, governance=guard,
        engine_factory=WorkflowEngine)
    # 生产原生入口(business_api.invoke)总以同一发行上下文签发的BusinessActor调用工作流领域；宿主Actor只留给后台提交/代理身份。
    f.__dict__.update(store=store, services=services, authority=authority, background=background,
                      runs=runs, guard=guard, agents=agents, api=api, actor=services.actor(authority))
    return f


# LLM: 构造隔离home下无治理/带治理两种终态发表恢复夹具，并挂接引擎执行观测钩子且禁止模型与脚本运行。
# 函数用途: 提供工作流终态发表与恢复回归测试的基础夹具。
@pytest.fixture(params=[False, True], ids=['plain', 'governed'])
def terminal(tmp_path, monkeypatch, request):
    assert Path(engine_module.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    home = replace(home_paths(tmp_path / 'home'), owner_id='alice', owner_provider='local', owner_kind='main')
    source = ConversationStore(home.owner_home_dir / 'conversations').threads.create(
        owner_id='alice', canonical_user_id='original-user')
    f = SimpleNamespace(home=home, governed=request.param, context=None, faults=[], resolution_state=None,
        host_actor=HostChannelActor('alice', source.thread_id, 'original-entry', 'local', 'local', 'user'),
        access={'allowed': True, 'denied': set(), 'review_allowed': True},
        submissions=[], completions=[], reviews=[], effects=[], resolutions=[], performances=[], native=[])
    _wire(f)
    perform, native = WorkflowEngine._perform, WorkflowEngine._resume_agent

    def observe_perform(self, record, spec, *args, **kwargs):
        f.performances.append(spec['step_id'])
        return perform(self, record, spec, *args, **kwargs)

    def observe_native(self, record, definition):
        f.native.append(record.workflow_run_id)
        return native(self, record, definition)

    monkeypatch.setattr(WorkflowEngine, '_perform', observe_perform)
    monkeypatch.setattr(WorkflowEngine, '_resume_agent', observe_native)
    monkeypatch.setattr(ScriptRunner, 'run_script', lambda *a, **k: pytest.fail('terminal recovery must not run scripts'))
    f.space = f.services.spaces.create({'name': '原空间'}, actor=f.services.actor(f.authority), idempotency_key='space')
    f.context = BusinessExecutionContext('alice', f.space['space_id'], None, 'stask-parent',
        f.space['revision'], None, (), (), 'business-context.v1')
    f.scope = {'scope_mode': 'spaces', 'space_ids': [f.space['space_id']]}
    f.api.execute('workflows.create', {'definition': _workflow('terminal-flow'), 'scope': f.scope},
        actor=f.actor, idempotency_key='create')
    return f


def start(f, workflow_ref='terminal-flow'):
    return f.api.execute('workflows.run', {'workflow_ref': workflow_ref, 'space_id': f.space['space_id']},
        actor=f.actor, idempotency_key=f'run-{workflow_ref}')


def resume(f, run_id, key, *, revision=None, actor=None, **request):
    current = f.runs.load_run(run_id)
    return f.api.execute('workflow_runs.resume', {'workflow_run_id': run_id, **request}, actor=actor or f.actor,
        expected_revision=current.revision if revision is None else revision, idempotency_key=key)


def complete(f, task_id):
    for status in ('accepted', 'done'):
        f.store.session_tasks.advance(task_id, SessionTaskUpdate(status=status,
            task_run_id='canonical-agent-run', result_refs=('canonical-result',)))


def tasks(f):
    rows, errors = f.store.session_tasks.list_report(limit=0)
    assert errors == []
    return rows


def counters(f):
    return (len(f.submissions), len(f.completions), tuple(f.performances), tuple(f.native),
            len(f.reviews), len(f.effects), len(f.resolutions))


def fail_terminal_once(f, monkeypatch, statuses):
    """只在原 Agent 步骤已有 canonical 完成后，对终态 run.json 发表前注入唯一一次 EIO。"""
    write = run_records._write_json

    def fail(root, parts, value):
        data = payload(value)
        if (parts[-1] == 'run.json' and data.get('status') in statuses and not f.faults
                and data['steps'][0]['escalation_result'].get('completion_ref')):
            f.faults.append(data)
            raise OSError(errno.EIO, 'one pre-publication terminal run.json EIO')
        return write(root, parts, value)

    monkeypatch.setattr(run_records, '_write_json', fail)


def terminal_eio(f, monkeypatch, workflow_ref='terminal-flow', statuses=('succeeded',)):
    """提交一个任务、读取一次完成、注入一次终态 EIO；返回原 base 已会持久化的暂停记录。"""
    receipt = start(f, workflow_ref)
    run_id = receipt['workflow_run_id']
    waiting = f.runs.load_run(run_id)
    assert receipt['state'] == 'pending' and waiting.status == 'waiting_for_agent'
    complete(f, waiting.steps[0].escalation_result['task_id'])
    fail_terminal_once(f, monkeypatch, statuses)
    before = f.runs.load_run(run_id)
    with pytest.raises(WorkflowExecutionError) as caught:
        resume(f, run_id, 'finish')
    paused = f.runs.load_run(run_id)
    assert len(f.faults) == 1 and f.submissions == [f.submissions[0]] and len(f.completions) == 1
    assert f.performances == ['agent'] and f.native == [run_id] and f.effects == f.resolutions == []
    assert len(f.reviews) == (1 if f.governed else 0)
    assert caught.value.record.workflow_run_id == run_id and caught.value.receipt['effect_outcome'] == 'unknown'
    for key in ('attempt_id', 'submission_key', 'task_id'):
        assert paused.steps[0].escalation_result[key] == waiting.steps[0].escalation_result[key]
    return SimpleNamespace(run_id=run_id, waiting=waiting, before=before, paused=paused)


def assert_affected_shape(case):
    """原 base 对单次终态 EIO 持久化的形状：步骤全确认，run 级 paused/unknown/needs_repair。"""
    paused = case.paused
    assert paused.status == 'paused_for_resume' and paused.needs_repair is True
    assert paused.phase == 'terminal' and paused.effect_outcome == 'unknown'
    assert {key: paused.breakpoint[key] for key in TERMINAL_BREAKPOINT} == TERMINAL_BREAKPOINT
    assert 'terminal run.json EIO' in paused.breakpoint['error_message']
    assert paused.current_step_id == paused.failed_step_id == ''
    step = paused.steps[0]
    assert step.status == 'succeeded' and step.output_snapshot == {'value': 7} and not step.needs_repair
    assert step.escalation_result['completion_ref'] == 'canonical-completion'
    assert step.escalation_result['effect_started'] is True
    assert paused.confirmed_step_refs == ('agent',) and paused.final_output == {'value': 7}


def assert_recovered(f, case, receipt, done):
    first = case.paused
    assert receipt['state'] == 'completed' and receipt['operation_id'] == case.run_id
    assert receipt['run']['workflow_run_id'] == case.run_id and receipt['run']['status'] == 'succeeded'
    assert receipt['run']['effect_outcome'] == 'committed'
    assert done.status == 'succeeded' and done.effect_outcome == 'confirmed' and done.phase == 'terminal'
    assert done.breakpoint is None and done.needs_repair is False and done.final_output == {'value': 7}
    assert (done.workflow_run_id, done.workflow_id, done.owner_id, done.started_at) == (
        first.workflow_run_id, first.workflow_id, first.owner_id, first.started_at)
    assert done.business_context == first.business_context and done.scope_snapshot == first.scope_snapshot
    assert done.execution_definition_ref == first.execution_definition_ref
    # 原步骤的状态、输出、attempt/task/completion/result 引用逐字段保持。
    assert done.steps == first.steps and done.confirmed_step_refs == ('agent',)
    rows = tasks(f)
    assert len(rows) == 1 and rows[0].task_id == first.steps[0].escalation_result['task_id']
    assert rows[0].status == 'done'


@pytest.mark.parametrize('restart', [False, True], ids=['same-service', 'restarted'])
def test_public_resume_publishes_original_terminal_after_single_eio(terminal, monkeypatch, restart):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    assert_affected_shape(case)
    view = f.api.execute('workflow_runs.get', {'workflow_run_id': case.run_id}, actor=f.actor)
    assert view['run']['status'] == 'paused_for_resume' and view['run']['phase'] == 'terminal'
    assert view['run']['confirmed_step_refs'] == ['agent']
    assert view['breakpoint']['reason_code'] == 'WORKFLOW_EXECUTION_FAILED' and view['breakpoint']['can_resume']
    if restart:
        _wire(f)
    marks = counters(f)
    receipt = resume(f, case.run_id, 'recover')
    done = f.runs.load_run(case.run_id)
    assert_recovered(f, case, receipt, done)
    # 补发终态零新增：不提交任务、不读完成、不进 _perform/_resume_agent、不审核、不对账。
    assert counters(f) == marks and len(f.faults) == 1
    again = resume(f, case.run_id, 'after-recovered')
    assert again['state'] == 'completed' and counters(f) == marks
    assert f.runs.load_run(case.run_id).steps == case.paused.steps


def test_healthy_control_finishes_original_run(terminal):
    f = terminal
    receipt = start(f)
    first = f.runs.load_run(receipt['workflow_run_id'])
    complete(f, first.steps[0].escalation_result['task_id'])
    receipt = resume(f, first.workflow_run_id, 'finish')
    done = f.runs.load_run(first.workflow_run_id)
    assert receipt['state'] == 'completed' and done.status == 'succeeded' and done.effect_outcome == 'confirmed'
    assert done.steps[0].output_snapshot == {'value': 7} and done.final_output == {'value': 7}
    assert done.steps[0].escalation_result['completion_ref'] == 'canonical-completion'
    assert len(f.submissions) == 1 and len(f.completions) == 1 and f.performances == ['agent']
    assert f.native == [first.workflow_run_id] and f.effects == f.resolutions == []
    assert len(f.reviews) == (1 if f.governed else 0) and len(tasks(f)) == 1


def test_same_key_retry_and_replay_keep_original_operation(terminal, monkeypatch):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    original = case.before.revision
    marks = counters(f)
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        resume(f, case.run_id, 'stale-new-key', revision=original)
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        resume(f, case.run_id, 'finish', revision=case.paused.revision)
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        f.api._engine(f.actor).resume_workflow_run(case.run_id, expected_revision=original, operation_key='finish',
            control_intent={'workflow_run_id': case.run_id, 'resolution_ref': 'different-intent'})
    assert f.runs.load_run(case.run_id).to_dict() == case.paused.to_dict()
    # 原控制键仍 pending：同键、同版本、同意图重试完成同一原操作。
    retried = resume(f, case.run_id, 'finish', revision=original)
    done = f.runs.load_run(case.run_id)
    assert_recovered(f, case, retried, done)
    replay = resume(f, case.run_id, 'finish', revision=original)
    assert replay == retried and f.runs.load_run(case.run_id).to_dict() == done.to_dict()
    raw = json.loads((f.home.owner_home_dir / 'data' / 'workflow_runs' / case.run_id / 'run.json').read_text())
    assert raw['control_operations']['finish']['state'] == 'completed'
    assert raw['control_operations']['finish']['expected_revision'] == original
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        f.api.execute('workflow_runs.cancel', {'workflow_run_id': case.run_id}, actor=f.actor,
            expected_revision=original, idempotency_key='finish')
    assert counters(f) == marks


@pytest.mark.parametrize('denied', ['workflows.run', 'workflow_runs.resume', 'foreign_actor'])
def test_revoked_execution_authorization_still_blocks_terminal_recovery(terminal, monkeypatch, denied):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    marks = counters(f)
    actor = replace(f.actor, owner_id='bob') if denied == 'foreign_actor' else f.actor
    f.access['denied'].add(denied)
    with pytest.raises(PermissionError, match='ACCESS_DENIED'):
        resume(f, case.run_id, 'revoked', actor=actor)
    assert f.runs.load_run(case.run_id).to_dict() == case.paused.to_dict() and counters(f) == marks
    f.access['denied'].clear()
    receipt = resume(f, case.run_id, 'restored')
    assert_recovered(f, case, receipt, f.runs.load_run(case.run_id))
    assert counters(f) == marks


@pytest.mark.parametrize('cancel_state', ['cancelling', 'cancelled'])
def test_cancelled_terminal_record_stays_non_resumable(terminal, monkeypatch, cancel_state):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    if cancel_state == 'cancelled':
        f.agents.cancel_handler = lambda task, *, actor, idempotency_key: {
            'state': 'completed', 'outstanding_refs': []}
    receipt = f.api.execute('workflow_runs.cancel', {'workflow_run_id': case.run_id}, actor=f.actor,
        expected_revision=f.runs.load_run(case.run_id).revision, idempotency_key='cancel')
    assert receipt['state'] == ('completed' if cancel_state == 'cancelled' else 'pending')
    cancelled = f.runs.load_run(case.run_id)
    assert cancelled.status == cancel_state
    marks = counters(f)
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        resume(f, case.run_id, 'after-cancel')
    current = f.runs.load_run(case.run_id)
    assert current.status == cancel_state and current.steps == cancelled.steps and counters(f) == marks


def test_repair_options_cannot_reopen_confirmed_prefix(terminal, monkeypatch):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    marks = counters(f)
    # 显式对账不能绑定不存在的当前步骤；两种模式都拒绝且不调用读取器。
    code = 'RECONCILIATION_REQUIRED' if f.governed else 'RESOLUTION_UNAVAILABLE'
    with pytest.raises(PermissionError, match=code):
        resume(f, case.run_id, 'resolution', resolution_ref='resolution-1')
    engine = f.api._engine(f.actor)
    for option in ({'from_step_id': 'agent'}, {'patched_step_output': {'value': 'client'}},
                   {'updated_data_sources': {}}):
        with pytest.raises(ValueError, match='WORKFLOW_TERMINAL_PUBLICATION_ONLY'):
            engine.resume_workflow_run(case.run_id, **option)
    current = f.runs.load_run(case.run_id)
    assert current.steps == case.paused.steps and current.status == 'paused_for_resume'
    assert f.resolutions == [] and counters(f) == marks
    receipt = resume(f, case.run_id, 'recover')
    assert_recovered(f, case, receipt, f.runs.load_run(case.run_id))
    assert counters(f) == marks


def test_terminal_recovery_still_enforces_frozen_output_contract(terminal, monkeypatch):
    f = terminal
    contract = {'outputs': ['value'], 'output_types': {'value': 'string'}}
    f.api.execute('workflows.create', {'definition': _workflow('typed-flow', contract), 'scope': f.scope},
        actor=f.actor, idempotency_key='typed-create')
    case = terminal_eio(f, monkeypatch, 'typed-flow', statuses=('failed',))
    paused = case.paused
    assert paused.status == 'paused_for_resume' and paused.phase == 'terminal'
    assert paused.steps[0].status == 'succeeded' and paused.steps[0].output_snapshot == {'value': 7}
    marks = counters(f)
    receipt = resume(f, case.run_id, 'recover')
    done = f.runs.load_run(case.run_id)
    # 各步骤成功不能代替冻结工作流输出合同。
    assert receipt['state'] == 'failed' and done.status == 'failed' and done.needs_repair is True
    assert done.breakpoint['error_code'] == 'WORKFLOW_OUTPUT_CONTRACT_VIOLATION'
    assert done.breakpoint['error_message'] == 'SCRIPT_OUTPUT_TYPE_MISMATCH'
    assert done.steps == paused.steps and counters(f) == marks


@pytest.mark.parametrize('state', ['committed', 'not_started'])
def test_bound_resolution_cannot_replace_confirmed_terminal_result(terminal, monkeypatch, state):
    f = terminal
    contract = {'outputs': ['value'], 'output_types': {'value': 'string'}}
    f.api.execute('workflows.create', {'definition': _workflow('typed-flow', contract), 'scope': f.scope},
        actor=f.actor, idempotency_key='typed-create')
    case = terminal_eio(f, monkeypatch, 'typed-flow', statuses=('failed',))
    # 输出合同失败已把 current_step 指向已确认末步；有效绑定的裁决也不能替换或重开它。
    assert case.paused.current_step_id == 'agent'
    f.resolution_state = state
    if f.governed:
        with pytest.raises(ValueError, match='WORKFLOW_TERMINAL_PUBLICATION_ONLY'):
            resume(f, case.run_id, 'bound-resolution', resolution_ref='resolution-1')
        assert f.resolutions == ['resolution-1'] and len(f.reviews) == 2
    else:
        with pytest.raises(PermissionError, match='RESOLUTION_UNAVAILABLE'):
            resume(f, case.run_id, 'bound-resolution', resolution_ref='resolution-1')
        assert f.resolutions == []
    current = f.runs.load_run(case.run_id)
    assert current.steps == case.paused.steps and current.status == 'paused_for_resume'
    marks = counters(f)
    receipt = resume(f, case.run_id, 'recover')
    done = f.runs.load_run(case.run_id)
    assert receipt['state'] == 'failed' and done.breakpoint['error_code'] == 'WORKFLOW_OUTPUT_CONTRACT_VIOLATION'
    assert done.steps == case.paused.steps and done.steps[0].output_snapshot == {'value': 7}
    assert counters(f) == marks


def test_terminal_recovery_rejects_step_set_that_differs_from_frozen_definition(terminal):
    f = terminal
    definition = _workflow('two-step-flow')
    definition['steps'].append({'step_id': 'tail', 'name': '后缀 Agent 步骤', 'mode': 'agent_task',
        'config': {'instruction': '不应执行'}})
    f.api.execute('workflows.create', {'definition': definition}, actor=f.actor, idempotency_key='two-step-create')
    step = StepExecutionRecord('agent', '原 Agent 步骤', 'agent_task', 'succeeded', 1, 2, 1, True,
        output_snapshot={'value': 7}, escalation_result={'attempt_id': 'original-attempt', 'effect_started': True,
            'task_id': 'original-task', 'submission_state': 'accepted', 'completion_ref': 'canonical-completion'})
    original = _terminal_shape(WorkflowRunRecord('truncated-run', 'two-step-flow', 'alice', 'manual', 'running',
        1, 2, 1, steps=(step,), business_context=f.context.to_dict(),
        scope_snapshot={'scope_mode': 'owner_global', 'space_ids': [], 'scope_revision': 1}, scope_revision=1))
    f.runs.create_run(original, f.api.store.get_workflow('two-step-flow').to_dict())
    # 记录本身满足分类；只有引擎锁内对冻结定义的步骤集核验能阻止 start=len 执行定义后缀。
    assert engine_module._requires_terminal_publication_resume(f.runs.load_run('truncated-run'))
    with pytest.raises(ValueError, match='WORKFLOW_TERMINAL_PUBLICATION_MISMATCH'):
        resume(f, 'truncated-run', 'truncated-resume')
    current = f.runs.load_run('truncated-run')
    assert current.status == 'paused_for_resume' and current.steps == original.steps
    assert f.submissions == f.completions == f.performances == f.native == [] and tasks(f) == []


def _terminal_shape(record, **changes):
    """复刻终态发表失败写下的 run 级暂停形状，只替换指定的步骤事实。"""
    shaped = replace_run_record(record, dict(status='paused_for_resume', needs_repair=True, current_step_id='',
        failed_step_id='', breakpoint={**TERMINAL_BREAKPOINT, 'error_message': 'terminal run.json EIO'}, **changes))
    execution_receipt(shaped, phase='terminal', effect_outcome='unknown')
    return shaped


@pytest.mark.parametrize('fact', ['unknown-script', 'pending-cleanup'])
def test_unresolved_script_effect_never_enters_terminal_recovery(terminal, fact):
    f = terminal
    definition = {'descriptor': {'id': 'script-flow', 'kind': 'workflow', 'name': '普通脚本',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'script', 'name': '脚本', 'mode': 'deterministic_script', 'script_id': 'never-run'}]}
    f.api.execute('workflows.create', {'definition': definition}, actor=f.actor, idempotency_key='script-create')
    result = {'attempt_id': 'original-script-attempt', 'effect_started': True}
    if fact == 'unknown-script':
        step = StepExecutionRecord('script', '脚本', 'deterministic_script', 'paused_for_resume', 1, 2, 1, False,
            script_id='never-run', needs_repair=True, escalation_result=result)
    else:
        step = StepExecutionRecord('script', '脚本', 'deterministic_script', 'succeeded', 1, 2, 1, False,
            script_id='never-run', output_snapshot={'value': 1},
            escalation_result={**result, 'cleanup_error': 'process group cleanup unverified'})
    original = _terminal_shape(WorkflowRunRecord(f'{fact}-run', 'script-flow', 'alice', 'manual', 'running', 1, 2, 1,
        steps=(step,), business_context=f.context.to_dict(),
        scope_snapshot={'scope_mode': 'owner_global', 'space_ids': [], 'scope_revision': 1}, scope_revision=1))
    f.runs.create_run(original, f.api.store.get_workflow('script-flow').to_dict())
    assert not engine_module._requires_terminal_publication_resume(f.runs.load_run(original.workflow_run_id))
    with pytest.raises(PermissionError, match='RECONCILIATION_REQUIRED'):
        resume(f, original.workflow_run_id, 'script-resume')
    current = f.runs.load_run(original.workflow_run_id)
    assert current.status == 'paused_for_resume' and current.effect_outcome == 'unknown'
    assert current.phase == 'terminal' and current.steps == original.steps
    assert f.submissions == f.completions == f.performances == f.native == []
    assert len(f.effects) == (1 if f.governed and fact == 'unknown-script' else 0)


def test_unresolved_agent_submission_keeps_native_reconciliation(terminal):
    f = terminal
    receipt = start(f)
    waiting = f.runs.load_run(receipt['workflow_run_id'])
    shaped = _terminal_shape(waiting)
    f.runs.save_run(shaped)
    assert not engine_module._requires_terminal_publication_resume(f.runs.load_run(waiting.workflow_run_id))
    receipt = resume(f, waiting.workflow_run_id, 'native-resume')
    current = f.runs.load_run(waiting.workflow_run_id)
    # 未完成的原任务只走原生完成链：不发表终态，不重提，不进 _perform。
    assert receipt['state'] == 'pending' and current.status == 'waiting_for_agent'
    assert current.steps[0].status == 'waiting_for_agent' and current.steps[0].output_snapshot == {}
    assert not current.steps[0].escalation_result.get('completion_ref')
    assert f.native == [waiting.workflow_run_id] and len(f.completions) == 1
    assert len(f.submissions) == 1 and f.performances == ['agent'] and len(tasks(f)) == 1


def _changed(record, change):
    data = record.to_dict()
    change(data)
    return run_records._decode_run(data)


def _step(data):
    return data['steps'][0]


def test_terminal_classification_is_narrow(terminal, monkeypatch):
    f = terminal
    case = terminal_eio(f, monkeypatch)
    accepts = engine_module._requires_terminal_publication_resume
    assert accepts(case.paused)
    # run 级 needs_repair、旧 unknown 或停止中断码都不单独否决已全部确认的终态发表。
    assert accepts(_changed(case.paused, lambda data: data['breakpoint'].update(error_code='WORKFLOW_PAUSED')))
    assert accepts(_changed(case.paused, lambda data: data.update(needs_repair=False)))
    rejected = {
        'agent-without-completion': lambda data: _step(data)['escalation_result'].pop('completion_ref'),
        'agent-waiting': lambda data: _step(data).update(status='waiting_for_agent'),
        'step-running': lambda data: _step(data).update(status='running'),
        'step-paused-unknown': lambda data: _step(data).update(status='paused_for_resume', needs_repair=True),
        'step-failed': lambda data: _step(data).update(status='failed'),
        'step-needs-repair': lambda data: _step(data).update(needs_repair=True),
        'pending-cleanup': lambda data: _step(data)['escalation_result'].update(cleanup_error='unverified'),
        'phase-finalize': lambda data: data.update(phase='finalize'),
        'breakpoint-other-phase': lambda data: data['breakpoint'].update(phase='step_publish'),
        'breakpoint-not-resumable': lambda data: data['breakpoint'].update(can_resume=False),
        'breakpoint-other-code': lambda data: data['breakpoint'].update(error_code='WORKFLOW_OUTPUT_CONTRACT_VIOLATION'),
        'step-breakpoint': lambda data: data.update(breakpoint={'step_index': 1, 'step_id': 'agent',
            'error_code': 'WORKFLOW_PAUSED', 'can_resume': True}),
        'no-breakpoint': lambda data: data.update(breakpoint=None),
        'no-steps': lambda data: data.update(steps=[]),
        **{f'status-{status}': (lambda value: lambda data: data.update(status=value))(status)
           for status in ('succeeded', 'failed', 'running', 'waiting_for_agent', 'cancelling', 'cancelled')},
    }
    for name, change in rejected.items():
        assert not accepts(_changed(case.paused, change)), name
