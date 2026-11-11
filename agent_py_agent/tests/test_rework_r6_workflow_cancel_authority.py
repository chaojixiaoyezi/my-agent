"""R6-R1：取消是控制动作；任务清理保留本次发行的实时授权。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowGovernance, workflow_candidate_hash
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
    plugin_observation_task,
    scoped_authorized,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request


# LLM: 用同一发行上下文签发的真实BusinessActor驱动工作流创建与启动，保留宿主Actor作为后台任务提交/取消身份。
# 函数用途: 构造等待后台代理阶段的可取消工作流运行夹具。
def make_workflow(host, *, governed=False, monkeypatch, source=None, workflow_id='r6-flow'):
    """用真实服务提交原 Agent 任务；禁止模型/脚本调用，只观察 submit。"""
    services = BusinessServices(host)
    store, owner = host.conversation_store, host.home_paths.owner_id
    source = source or store.threads.create(owner_id=owner, owner_home=str(services.owner_home))
    actor = HostChannelActor(owner, source.thread_id, 'original-request', 'local', 'local', 'human')
    access, calls, submissions, handlers = {'denied': set()}, [], [], []
    native_context = bind_business_context(host, actor=actor, authorize=lambda _a, _f: True)
    background = _BusinessBackgroundService(services, native_context)
    space = services.spaces.create({'name': '原工作流空间'}, actor=services.actor(native_context), idempotency_key='r6-space')
    # 生产原生入口(business_api.invoke)总以同一发行上下文签发的BusinessActor调用工作流领域；宿主Actor只留给后台提交/代理身份。
    caller = services.actor(native_context)
    runs = WorkflowRunStore(services.owner_home)
    submit = background.submit

    def observe_submit(*args, **kwargs):
        submissions.append(kwargs['idempotency_key'])
        return submit(*args, **kwargs)

    def cancel_handler(task_id, *, actor, idempotency_key):
        handlers.append((task_id, actor, idempotency_key))
        return background.cancel_task(task_id, actor=actor, include_descendants=True)

    # LLM: 记录工作流授权动作并同时支持原生BusinessActor与插件签发BusinessActor校验。
    # 函数用途: 按owner、拒绝集合与Actor自身授权判断工作流动作是否允许。
    def authorize(current, action, ref):
        calls.append(action)
        allowed = getattr(current, 'owner_id', None) == owner and action not in access['denied']
        return allowed and (current is caller or current.authorize(action, {'workflow_ref': ref}) is True)

    def review(ref, run, step_id):
        step = next(row for row in run.steps if row.step_id == step_id)
        return {'receipt_ref': ref, 'owner_id': owner, 'workflow_run_id': run.workflow_run_id,
            'task_id': run.business_context['task_id'], 'step_id': step_id,
            'attempt_id': step.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(runs.load_workflow_snapshot(run.workflow_run_id)),
            'author_id': 'writer', 'reviewer_id': 'reviewer', 'decision': 'approved', 'expires_at': 9999999999}

    guard = WorkflowGovernance(runs, policy_provider=lambda *_: {'author_id': 'writer',
        'review_refs': ['review-one'], 'minimum_reviews': 1}, receipt_reader=review,
        authorization_check=lambda *_: True) if governed else None
    monkeypatch.setattr(background, 'submit', observe_submit)
    monkeypatch.setattr(ScriptRunner, 'run_script', lambda *a, **k: pytest.fail('cancellation ran a script'))
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor,
        completion_reader=lambda *a, **k: None, cancel_handler=cancel_handler)
    context = BusinessExecutionContext(owner, space['space_id'], None, 'original-workflow-task',
        space['revision'], None, (), (), 'business-context.v1')
    api = WorkflowScopeService(services.owner_home, owner_id=owner, authorize=authorize,
        context_provider=lambda *_: context, agent_steps=agents, governance=guard, engine_factory=WorkflowEngine)
    definition = {'descriptor': {'id': workflow_id, 'kind': 'workflow', 'name': '原工作流',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'agent', 'name': '原 Agent 步骤', 'mode': 'agent_task',
            'config': {'instruction': '只核对原任务'}}]}
    api.execute('workflows.create', {'definition': definition}, actor=caller, idempotency_key='r6-create')
    result = api.execute('workflows.run', {'workflow_ref': workflow_id}, actor=caller, idempotency_key='r6-start')
    run = runs.load_run(result['workflow_run_id'])
    assert run.status == 'waiting_for_agent' and len(submissions) == 1
    task = store.session_tasks.require(run.steps[0].escalation_result['task_id'])
    services.workflows = api
    return SimpleNamespace(host=host, store=store, services=services, background=background, actor=caller,
        host_actor=actor,
        agents=agents, api=api, runs=runs, run=run, task=task, access=access, calls=calls,
        submissions=submissions, handlers=handlers, space=space, definition=definition)


@pytest.fixture(params=[False, True], ids=['plain', 'governed'])
def native_workflow(tmp_path, monkeypatch, request):
    home = replace(home_paths(tmp_path / 'isolated-home'), owner_id='local/main',
        owner_provider='local', owner_kind='main')
    host = SimpleNamespace(home_paths=home, conversation_store=ConversationStore(home.owner_home_dir / 'conversations'),
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'),
        run=lambda *a, **k: pytest.fail('cancellation ran a model'))
    return make_workflow(host, governed=request.param, monkeypatch=monkeypatch)


def cancel(case, *, actor=None, revision=None, key='r6-cancel'):
    return case.api.execute('workflow_runs.cancel', {'workflow_run_id': case.run.workflow_run_id},
        actor=actor or case.actor, expected_revision=case.run.revision if revision is None else revision,
        idempotency_key=key)


def test_control_permission_cancels_original_without_execute(native_workflow):
    case = native_workflow
    case.access['denied'].add('workflows.run')
    case.calls.clear()
    result = cancel(case)
    assert result == {'operation_id': case.run.workflow_run_id, 'state': 'completed', 'outstanding_refs': []}
    assert case.runs.load_run(case.run.workflow_run_id).status == 'cancelled'
    assert case.store.session_tasks.require(case.task.task_id).status == 'cancelled'
    assert case.store.guidance.receipt(case.task.body_dedupe_key).status == 'rejected'
    assert len(case.submissions) == 1 and len(case.handlers) == 1
    assert 'workflows.run' not in case.calls
    assert 'workflow_runs.cancel' in case.calls
    from agent_py_agent.agent.workflows import engine
    assert Path(engine.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize('denied', ['workflow_runs.cancel', 'foreign-owner', 'stale-revision'])
def test_native_cancel_preserves_denial_owner_and_cas(native_workflow, denied):
    case = native_workflow
    before = case.runs.load_run(case.run.workflow_run_id).to_dict()
    if denied == 'workflow_runs.cancel':
        case.access['denied'].add(denied)
    options = ({'actor': replace(case.actor, owner_id='another-owner')} if denied == 'foreign-owner' else
        {'revision': case.run.revision - 1} if denied == 'stale-revision' else {})
    with pytest.raises(ValueError if denied == 'stale-revision' else PermissionError):
        cancel(case, **options)
    assert case.runs.load_run(case.run.workflow_run_id).to_dict() == before
    assert case.store.session_tasks.require(case.task.task_id) == case.task
    assert len(case.submissions) == 1 and case.handlers == []


def test_native_cancel_replay_preserves_original_control_and_execution_lock(native_workflow):
    case = native_workflow
    with case.runs.execution_lock(case.run.workflow_run_id), pytest.raises(RuntimeError, match='正在执行'):
        cancel(case)
    result = cancel(case)
    before = case.runs.load_run(case.run.workflow_run_id).to_dict()
    assert cancel(case) == result
    assert case.runs.load_run(case.run.workflow_run_id).to_dict() == before
    assert len(case.handlers) == 1 and len(case.submissions) == 1


@pytest.fixture(params=[None, 'alice'], ids=['main', 'ordinary-owner'])
def plugin_workflow(production_host, monkeypatch, request):
    from itertools import count

    from agent_py_agent.agent.plugin_host import http_transport
    ticks = count(start=1000, step=2)
    monkeypatch.setattr(http_transport, 'time', SimpleNamespace(
        **{**vars(http_transport.time), 'monotonic': lambda: next(ticks)}))
    user = request.param
    if user:
        production_call(production_host, 'users.local.create', {'user_id': user},
            key='round-six-workflow-user', status=201)
    services, seed = plugin_observation_task(production_host, user=user)
    services.host.config.model_backend = 'echo'
    production_call(production_host, 'business.controllers.ensure', user=user, key='round-six-visible-controller')
    # 原执行宿主与插件scoped host是不同对象；只有原宿主拥有真正的停止依赖。
    original_host = SimpleNamespace(home_paths=services.host.home_paths, config=services.host.config,
        conversation_store=services.host.conversation_store,
        run=lambda *a, **k: pytest.fail('plugin cancellation ran a model'))
    case = make_workflow(original_host, monkeypatch=monkeypatch,
        source=original_host.conversation_store.threads.require(seed.sender_thread_id))
    services.workflows = case.api
    case.services, case.plugin, case.user = services, production_host, user
    return case


def plugin_cancel(case, *, run=None, key='round-six-workflow-cancel'):
    run = run or case.run
    row = {'schema': 'plugin_host_call.v2', 'action': 'business.workflow_runs.cancel',
        'payload': {'workflow_run_id': run.workflow_run_id, 'expected_revision': run.revision},
        'idempotency_key': key}
    if case.user:
        row['as_user'] = {'user_id': case.user}
    return http_request(case.plugin[0], case.plugin[1], row)


def dependency_snapshot(case):
    return (case.services.workflows, dict(vars(case.api)), case.api.agent_steps, dict(vars(case.agents)))


def test_issued_plugin_http_cancel_supported_and_execution_still_unavailable(plugin_workflow):
    case = plugin_workflow
    before = dependency_snapshot(case)
    caps = production_call(case.plugin, 'business.system.capabilities', user=case.user)['supported_actions']
    assert 'workflow_runs.cancel' in caps
    assert 'workflows.run' not in caps and 'workflow_runs.resume' not in caps
    status, result = plugin_cancel(case)
    assert status == 200 and result['data']['state'] == 'completed', result
    assert result['data']['outstanding_refs'] == []
    assert case.store.session_tasks.require(case.task.task_id).status == 'cancelled'
    assert case.store.guidance.receipt(case.task.body_dedupe_key).status == 'rejected'
    assert case.runs.load_run(case.run.workflow_run_id).status == 'cancelled'
    assert len(case.submissions) == 1 and case.handlers == [], 'must not forward to stored native handler'
    assert dependency_snapshot(case) == before
    for action, params in [('workflows.run', {'workflow_ref': case.run.workflow_id}),
            ('workflow_runs.resume', {'workflow_run_id': case.run.workflow_run_id, 'expected_revision': 0})]:
        # 原插件执行门仍要求普通用户，主安装的执行请求也不能落入native路径。
        if not case.user:
            production_call(case.plugin, 'users.local.create', {'user_id': 'alice'},
                key='round-six-execution-user', status=201)
        row = {'schema': 'plugin_host_call.v2', 'action': 'business.' + action,
            'as_user': {'user_id': case.user or 'alice'}, 'payload': params,
            'idempotency_key': 'round-six-denied-' + action.replace('.', '-')}
        status, result = http_request(case.plugin[0], case.plugin[1], row)
        assert status == 503 and result['error']['code'] == 'HOST_NOT_READY', result


def task_snapshot(case, task):
    receipt = case.store.guidance.receipt(task.body_dedupe_key)
    return (case.store.session_tasks.require(task.task_id).to_dict(), receipt.to_dict() if receipt else None,
        [row.to_dict() for row in case.store.wakes.pending(limit=0)
            if row.metadata.get('session_task_id') == task.task_id],
        case.store.claims.load_report(task.target_thread_id))


# LLM: 宿主原生身份创建的私有子任务不在插件可见范围；插件取消不得越权清理私有后代任务，运行保持cancelling待原生收回。
# 函数用途: 验证工作流插件取消不会清理原生宿主私有的后代后台任务。
def test_workflow_plugin_cannot_clean_private_native_descendant(plugin_workflow):
    case = plugin_workflow
    target = case.background.create({'space_id': case.space['space_id'], 'name': '原生私有后代'},
        actor=case.host_actor, idempotency_key='round-six-private-worker')['agent']['thread_id']
    child_id = case.background.assign({'space_id': case.space['space_id'], 'agent_id': target,
        'instruction': '不可取消的原生后代', 'origin_task_id': case.task.task_id},
        actor=case.host_actor, idempotency_key='round-six-private-descendant')['task_id']
    child = case.store.session_tasks.require(child_id)
    before, root, dependencies = task_snapshot(case, child), task_snapshot(case, case.task), dependency_snapshot(case)
    status, result = plugin_cancel(case)
    assert status == 404 and result['error']['code'] == 'RESOURCE_NOT_FOUND', result
    assert task_snapshot(case, child) == before and task_snapshot(case, case.task) == root
    current = case.runs.load_run(case.run.workflow_run_id)
    assert current.status == 'cancelling' and current.steps[0].escalation_result['reconciliation_state'] == 'confirmed'
    assert not current.steps[0].escalation_result.get('cancellation_receipt')
    assert len(case.submissions) == 1 and case.handlers == []
    assert dependency_snapshot(case) == dependencies
