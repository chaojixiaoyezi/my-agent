"""真实native工作流→SessionTask→后台scheduler/core；只替换底层模型。"""
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ToolOperationAuthorityRequest,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import flow
from agent_py_agent.tests.test_acceptance_workflow_native_ports import wire as wire
from agent_py_agent.tests.test_business_v12_background_scheduling import add_echo_profile


def authority(agent, params):
    return ToolOperationAuthorityRequest(owner_id=agent.home_paths.owner_id,
        run_id=params.run_id, task_id=params.task_id, attempt_id=params.attempt_id,
        operation_id='native-background-observation', tool_name='workflows')


def test_native_waiting_real_background_completion_and_resume(wire, monkeypatch):
    agent, client = wire
    # 原宿主装配开关；不直接注入后台端口或改canonical任务。
    agent.business_host_ports_enabled = True
    add_echo_profile(agent)
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.session_task_dispatch import SessionTaskDispatchError
    errors = []
    original_submit = BackgroundAgentService.submit

    def submit(self, *args, **kwargs):
        try:
            return original_submit(self, *args, **kwargs)
        except SessionTaskDispatchError as exc:
            errors.append({'output': exc.outcome.output, 'envelope': exc.outcome.result_envelope})
            print('dispatch_failure', errors[-1])
            raise

    monkeypatch.setattr(BackgroundAgentService, 'submit', submit)
    original_wake = agent.conversation_store.wakes.raise_signal

    def wake(*args, **kwargs):
        try:
            return original_wake(*args, **kwargs)
        except Exception:
            import traceback
            print(traceback.format_exc())
            raise

    monkeypatch.setattr(agent.conversation_store.wakes, 'raise_signal', wake)
    import agent_py_agent.agent.agent_core.host_turn_scope as host_scopes
    from agent_py_agent.agent.agent_core import _tool_loop_service as model_loop

    foreground = []
    original_scope = host_scopes.host_turn_scope

    @contextmanager
    def observe_scope(*args, **kwargs):
        # 旁观真实scope，不延长它的寿命，也不向另一个线程搬运临时属性。
        with original_scope(*args, **kwargs) as turn:
            if kwargs.get('reason') == 'workflow_execution':
                foreground.append(turn.params)
            yield turn

    monkeypatch.setattr(host_scopes, 'host_turn_scope', observe_scope)
    space = client.call('spaces.create', {'name': 'native-waiting'})['data']['space_id']
    definition = flow('native-background', [{'step_id': 'work', 'name': 'work', 'mode': 'agent_task',
        'on_error': 'fail', 'config': {'instruction': 'Return the offline final result.'}},
        {'step_id': 'after', 'name': 'after', 'mode': 'llm_skill_analysis', 'on_error': 'fail'}])
    assert client.call('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}})['ok']
    result = client.call('workflows.run', {'workflow_ref': 'native-background', 'space_id': space})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run_id = result['data']['workflow_run_id']
    waiting = store.load_run(run_id)
    assert waiting.status == 'waiting_for_agent', (result, waiting.to_dict())
    assert waiting.steps[0].escalation_result['submission_state'] == 'accepted', errors
    assert len(foreground) == 1 and foreground[0].run_id == run_id
    with pytest.raises(AuthorityContextMissing):
        agent.tools.operation_store.require_authority(authority(agent, foreground[0]))
    task_id = waiting.steps[0].escalation_result['task_id']
    task = agent.conversation_store.session_tasks.require(task_id)
    reached, release = Event(), Event()
    identities = []
    original_model_request = model_loop.generate_model_response

    def observe_model_request(request):
        # 当前运行参数属于真正执行模型轮的线程，而非底层超时保护的生成线程。
        params = request.agent._current_run_params
        identities.append(params)
        request.agent.tools.operation_store.require_authority(authority(request.agent, params))
        response = original_model_request(request)
        request.agent.tools.operation_store.require_authority(authority(request.agent, params))
        return response

    monkeypatch.setattr(model_loop, 'generate_model_response', observe_model_request)

    def generate(self, prompt, **kwargs):
        reached.set()
        assert release.wait(10)
        return ModelResponse(text='The offline task is complete.', backend='echo')

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store,
        channels=agent.delivery_service)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': agent.conversation_store})
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(scheduler.tick_thread, task.target_thread_id, now=time.time())
        try:
            assert reached.wait(10), future.result(1) if future.done() else 'background has not reached model'
            assert len(identities) == 1 and identities[0].run_id != run_id
            assert identities[0].attempt_id and identities[0].attempt_id != foreground[0].attempt_id
            active = agent.conversation_store.session_tasks.require(task_id)
            # SessionTask保存的是原SQLite TaskRun链接，不存在current_run_id字段。
            execution = agent.subagents.runtime_db.agent_run_for_run_id(identities[0].run_id)
            assert execution is not None and active.task_run_id == execution['task_run_id']
            assert execution['current_attempt_id'] == identities[0].attempt_id
            agent.tools.operation_store.require_authority(authority(agent, identities[0]))
            with pytest.raises(AuthorityContextMissing):
                agent.tools.operation_store.require_authority(authority(agent, foreground[0]))
            assert store.load_run(run_id).status == 'waiting_for_agent'
        finally:
            release.set()
        reports = future.result(20)
    assert reports and reports[0].runtime_status == 'ok', reports
    done = agent.conversation_store.session_tasks.require(task_id)
    assert done.status == 'done', done
    with pytest.raises(AuthorityContextMissing):
        agent.tools.operation_store.require_authority(authority(agent, identities[0]))
    agent.backend.generate_structured = lambda *args, **kwargs: ModelResponse(text='{"value":"after"}', backend='echo')
    resumed = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run_id},
        expected_revision=waiting.revision))
    final = store.load_run(run_id)
    assert final.status == 'succeeded', (resumed, final.to_dict())
    assert final.steps[0].escalation_result['task_id'] == task_id
    assert len(identities) == 1
    assert len(foreground) == 2 and foreground[1].attempt_id != foreground[0].attempt_id
    for params in (*foreground, *identities):
        with pytest.raises(AuthorityContextMissing):
            agent.tools.operation_store.require_authority(authority(agent, params))


def test_default_builder_requires_real_host_actor_without_inventing_thread(wire):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.workflows.agent_steps import build_default_workflow_agent_steps

    agent, _ = wire
    port = build_default_workflow_agent_steps(agent)
    assert type(port.background_service) is BackgroundAgentService
    assert port.background_service.store is agent.conversation_store
    before = agent.conversation_store.threads.list(limit=100)
    with pytest.raises(PermissionError, match='WORKFLOW_AGENT_ACTOR_REQUIRED'):
        port._actor(SimpleNamespace(owner_id=agent.home_paths.owner_id, workflow_id='not-a-thread'))
    assert agent.conversation_store.threads.list(limit=100) == before


def test_native_unknown_dispatch_reconciles_same_task_without_redispatch(wire, monkeypatch):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps

    agent, client = wire
    agent.business_host_ports_enabled = True
    add_echo_profile(agent)
    space = client.call('spaces.create', {'name': 'unknown-dispatch'})['data']['space_id']
    calls = []
    original = WorkflowAgentSteps.submit

    def submit(self, run, spec, step):
        actor = self._actor(run)
        assert isinstance(actor, HostChannelActor)
        assert agent.conversation_store.threads.require(actor.source_thread_id).owner_id == run.owner_id
        calls.append((self, self.background_service, actor, step.escalation_result['attempt_id']))
        return original(self, run, spec, step)

    # 原服务先创建canonical task/body，再在原wake发表口注入故障；不Mock后台，不补任务状态。
    def publication_failure(*args, **kwargs):
        raise OSError('offline wake publication interrupted')

    monkeypatch.setattr(WorkflowAgentSteps, 'submit', submit)
    monkeypatch.setattr(agent.conversation_store.wakes, 'raise_signal', publication_failure)
    definition = flow('unknown-background', [{'step_id': 'work', 'name': 'work', 'mode': 'agent_task',
        'on_error': 'fail', 'config': {'instruction': 'Return the offline final result.'}}])
    assert client.call('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}})['ok']
    result = client.call('workflows.run', {'workflow_ref': 'unknown-background', 'space_id': space})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run_id = result['data']['workflow_run_id']
    waiting = store.load_run(run_id)
    attempt = waiting.steps[0].escalation_result['attempt_id']
    task_id = waiting.steps[0].escalation_result['task_id']
    assert waiting.status == 'waiting_for_agent'
    assert waiting.steps[0].escalation_result['submission_state'] == 'unknown'
    assert agent.conversation_store.session_tasks.require(task_id).status == 'queued'
    for _ in range(2):
        current = store.load_run(run_id)
        resumed = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run_id},
            expected_revision=current.revision))
        assert resumed['ok'], resumed
        current = store.load_run(run_id)
        assert current.status == 'waiting_for_agent'
        assert current.steps[0].escalation_result['task_id'] == task_id
        assert current.steps[0].escalation_result['attempt_id'] == attempt
    assert len(calls) == 1
    # 第二次独立请求不得给第一条运行或默认缓存重绑actor/service。
    again = client.call('workflows.run', {'workflow_ref': 'unknown-background', 'space_id': space})
    other = store.load_run(again['data']['workflow_run_id'])
    assert other.workflow_run_id != run_id and len(calls) == 2
    assert calls[0][0] is not calls[1][0] and calls[0][1] is not calls[1][1]
    assert calls[0][2].source_thread_id == calls[1][2].source_thread_id
    assert calls[0][2].entry_request_id != calls[1][2].entry_request_id
    assert calls[0][3] == attempt


@pytest.mark.parametrize('running', [False, True], ids=['queued', 'model-running'])
def test_native_waiting_cancellation_uses_real_exit_proof(wire, monkeypatch, running):
    from agent_py_agent.agent.agent_core import _tool_loop_service as model_loop

    agent, client = wire
    agent.business_host_ports_enabled = True
    add_echo_profile(agent)
    reached, release, exited = Event(), Event(), Event()
    identities, generations = [], []
    original_request = model_loop.generate_model_response

    def observe_request(request):
        params = request.agent._current_run_params
        identities.append(params)
        request.agent.tools.operation_store.require_authority(authority(request.agent, params))
        return original_request(request)

    def generate(self, prompt, **kwargs):
        generations.append(prompt)
        reached.set()
        try:
            assert release.wait(20)
            return ModelResponse(text='A late response must not revive cancelled work.', backend='echo')
        finally:
            exited.set()

    monkeypatch.setattr(model_loop, 'generate_model_response', observe_request)
    monkeypatch.setattr(EchoBackend, 'generate', generate)
    space = client.call('spaces.create', {'name': 'native-cancel'})['data']['space_id']
    definition = flow('native-cancel', [{'step_id': 'work', 'name': 'work', 'mode': 'agent_task',
        'on_error': 'fail', 'config': {'instruction': 'Return the offline final result.'}},
        {'step_id': 'after', 'name': 'after', 'mode': 'llm_skill_analysis', 'on_error': 'fail'}])
    assert client.call('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}})['ok']
    started = client.call('workflows.run', {'workflow_ref': 'native-cancel', 'space_id': space})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run_id = started['data']['workflow_run_id']
    waiting = store.load_run(run_id)
    assert waiting.status == 'waiting_for_agent'
    assert waiting.steps[0].escalation_result['submission_state'] == 'accepted'
    task_id = waiting.steps[0].escalation_result['task_id']
    original_attempt = waiting.steps[0].escalation_result['attempt_id']
    task = agent.conversation_store.session_tasks.require(task_id)
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store,
        channels=agent.delivery_service)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': agent.conversation_store})
    cancel = client.prepare('workflow_runs.cancel', {'workflow_run_id': run_id},
        expected_revision=waiting.revision)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(scheduler.tick_thread, task.target_thread_id, now=time.time()) if running else None
        try:
            if running:
                assert reached.wait(10), future.result(1) if future.done() else 'model not reached'
                assert len(identities) == len(generations) == 1
                agent.tools.operation_store.require_authority(authority(agent, identities[0]))
            first = client.send(cancel)
            assert first['ok'], first
            current = store.load_run(run_id)
            receipt = current.steps[0].escalation_result['cancellation_receipt']
            if running:
                # 模型worker尚未物理退出，关闭权限不等于完成取消。
                assert first['data']['state'] == receipt['state'] == 'pending'
                assert first['data']['outstanding_refs'] == [task_id]
                assert current.status == 'cancelling'
                assert receipt['outstanding_refs']
                with pytest.raises(AuthorityContextMissing):
                    agent.tools.operation_store.require_authority(authority(agent, identities[0]))
            else:
                assert first['data']['state'] == receipt['state'] == 'completed'
                assert first['data']['outstanding_refs'] == receipt['outstanding_refs'] == []
                assert current.status == 'cancelled'
        finally:
            release.set()
        if future is not None:
            reports = future.result(20)
            # 原runtime.py的取消分支结案wake且不生成正常交付报告。
            assert reports == []
            assert exited.wait(5)
            claim, error = agent.conversation_store.claims.load_report(task.target_thread_id)
            assert error is None and claim['finished_at']
            assert claim['status'] in {'finished', 'failed', 'cancelled'}
    # 只重送原取消请求来对账，不调用内部结算器或直接写任务终态。
    confirmed = client.send(cancel)
    assert confirmed['ok'] and confirmed['data']['state'] == 'completed', confirmed
    assert confirmed['data']['outstanding_refs'] == []
    final = store.load_run(run_id)
    assert final.status == 'cancelled'
    assert final.steps[0].escalation_result['task_id'] == task_id
    assert final.steps[0].escalation_result['attempt_id'] == original_attempt
    assert final.steps[0].escalation_result['cancellation_receipt']['outstanding_refs'] == []
    assert final.steps[1].status == 'pending'
    assert agent.conversation_store.session_tasks.require(task_id).status == 'cancelled'
    for params in identities:
        with pytest.raises(AuthorityContextMissing):
            agent.tools.operation_store.require_authority(authority(agent, params))
    replay = client.send(cancel)
    assert replay['ok'] and replay['data'] == confirmed['data']
    assert len(generations) == int(running)


@pytest.mark.parametrize('fault', ['owner', 'home', 'store', 'unavailable', 'post-lease-denial'])
def test_native_cancel_host_lease_rejects_drift_before_mutation(wire, monkeypatch, fault):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import business_api
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    agent, client = wire
    agent.business_host_ports_enabled = True
    add_echo_profile(agent)
    space = client.call('spaces.create', {'name': 'lease-guards'})['data']['space_id']
    definition = flow('lease-guards', [{'step_id': 'work', 'name': 'work', 'mode': 'agent_task',
        'on_error': 'fail', 'config': {'instruction': 'Return the offline final result.'}}])
    assert client.call('workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}})['ok']
    started = client.call('workflows.run', {'workflow_ref': 'lease-guards', 'space_id': space})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    waiting = store.load_run(started['data']['workflow_run_id'])
    assert waiting.status == 'waiting_for_agent'
    task_id = waiting.steps[0].escalation_result['task_id']
    task = agent.conversation_store.session_tasks.require(task_id)
    assert task.status == 'queued'
    lent, released = [], []
    original_check = business_api.BusinessCallContext.check

    def check(context, action, facts):
        original_check(context, action, facts)
        # 在真实授权边界注入租用后的拒绝，不能沿租用前的许可继续取消。
        if fault == 'post-lease-denial' and lent and action == 'workflow_runs.cancel':
            raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)

    def provide(server, owner, stack):
        lent.append(owner)
        stack.callback(released.append, owner)
        if fault == 'unavailable':
            raise HostAPIError('HOST_NOT_READY', 503)
        if fault == 'post-lease-denial':
            return agent
        paths = SimpleNamespace(owner_id=agent.home_paths.owner_id, owner_home_dir=agent.home_paths.owner_home_dir)
        conversation_store = agent.conversation_store
        if fault == 'owner':
            paths.owner_id = 'another-owner'
        elif fault == 'home':
            paths.owner_home_dir = agent.home_paths.owner_home_dir / 'other-home'
        elif fault == 'store':
            conversation_store = SimpleNamespace(storage=SimpleNamespace(
                root=agent.conversation_store.storage.root / 'other-store'))
        return SimpleNamespace(home_paths=paths, conversation_store=conversation_store)

    monkeypatch.setattr(business_api.BusinessCallContext, 'check', check)
    monkeypatch.setattr(business_api, 'owner_complete_host', provide)
    result = client.send(client.prepare('workflow_runs.cancel', {'workflow_run_id': waiting.workflow_run_id},
        expected_revision=waiting.revision))
    assert not result['ok'] and result['effect_outcome'] == 'not_started', result
    assert result['error']['code'] == ('BUSINESS_ACCESS_DENIED' if fault == 'post-lease-denial' else 'HOST_NOT_READY')
    assert lent == released and len(lent) == 1
    assert store.load_run(waiting.workflow_run_id).to_dict() == waiting.to_dict()
    assert agent.conversation_store.session_tasks.require(task_id) == task
