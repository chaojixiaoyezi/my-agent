"""R9-R2 P1-R9-02：W2 控制在原 live 授权后取准确 owner 完整宿主，租用跨整个控制并在所有路径释放。

真实 Gateway HTTP、原 owner 池、原调度器、claim、RuntimeDB 与原取消链；只替换外部模型。
"""
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_call,
    create_background,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)

USER = 'business-alice'


class _BlockingModel:
    """第一轮真实模型调用阻塞到测试放行；之后普通返回。"""

    name = 'r9-native-control-model'

    def __init__(self):
        self.calls = 0
        self.waiting, self.release, self.returned = Event(), Event(), Event()

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
        return ProviderToolCapability(provider=self.name, endpoint='local://test', model='', stream=False,
            native_supported=True, evidence='contract_test', observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        from agent_py_agent.agent.backends import ModelResponse
        self.calls += 1
        if self.calls == 1:
            self.waiting.set()
            assert self.release.wait(15), 'test did not release the original model call'
            self.returned.set()
        return ModelResponse(text='original result', backend=self.name)


# LLM: 限流属于每个真实HostTransport，不改生产配额或admit；只在测试安排阶段结束后清空该宿主桶。
# 函数用途: 让被测控制请求不受准备空间/线程所消费的写入额度干扰。
@pytest.fixture
def reset_business_limiter():
    def reset(server):
        limiter = server.plugin_host.limiter
        with limiter._lock:
            limiter._rows.clear()
    return reset


@pytest.fixture
def scoped_gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'native-control-home'))
    base = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False, business_spaces_enabled=True,
        memory_path='memory.jsonl', request_timeout=0, prompt_files=[]), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(base.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=base))
    server.start()
    try:
        yield server, base
    finally:
        server.stop()


def _running_scoped_task(native):
    """经真实HTTP派发；原owner池实例上的原调度器把任务跑到阻塞的模型调用。"""
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    server, base = native
    params, agent = create_background(native)
    status, assigned = background_call(native, 'tasks.assign', {'agent_id': agent['agent_id'],
        'space_id': params['space_id'], 'instruction': '保持运行直到取消'}, idempotency_key='r9-control-task')
    assert status == 200, assigned
    pool = _owner_pool(base)
    owner = OwnerIdentity.provider_user('feishu', USER)
    owner_agent = pool.get(owner)
    owner_agent.backend = model = _BlockingModel()
    store = owner_agent.conversation_store
    scheduler = BackgroundMainAgentScheduler({'runtime': BackgroundMainAgentRuntime(agent=owner_agent, store=store),
                                              'store': store})
    outcome = {}

    def tick():
        try:
            outcome['reports'] = scheduler.tick()
        except BaseException as error:  # noqa: BLE001 - 交给测试线程断言
            outcome['error'] = error

    thread = Thread(target=tick, name='r9-native-control-slice')
    thread.start()
    case = SimpleNamespace(server=server, base=base, pool=pool, owner=owner, owner_agent=owner_agent, model=model,
        store=store, repo=owner_agent.subagents.runtime_db, thread=thread, outcome=outcome, view=agent,
        task_id=assigned['data']['task_id'])
    try:
        assert model.waiting.wait(15), 'original scheduler did not reach the model call'
        task = store.session_tasks.require(case.task_id)
        case.target = task.target_thread_id
        case.claim = store.claims.load(case.target)
        assert case.claim['status'] == 'running'
        case.authority = case.claim['runtime_authority']
    except BaseException:
        _finish(case)
        raise
    return case


def _finish(case):
    case.model.release.set()
    case.thread.join(timeout=15)
    assert not case.thread.is_alive(), 'original scheduler slice did not return'
    assert 'error' not in case.outcome, case.outcome.get('error')


def _control(case, action, key):
    """原冻结请求封套：tasks.cancel 必带原修订，执行取消只认准确 run/attempt。"""
    if action == 'executions.cancel':
        return {'params': {'run_id': case.authority['run_id'], 'attempt_id': case.authority['attempt_id'],
                           'include_descendants': True}, 'idempotency_key': key}
    task = case.store.session_tasks.require(case.task_id)
    return {'params': {'task_id': case.task_id, 'include_descendants': True}, 'idempotency_key': key,
            'expected_revision': int(task.updated_at * 1000000)}


def _call_control(native, case, action, key):
    request = _control(case, action, key)
    return background_call(native, action, request.pop('params'), **request)


def _pins(case):
    return case.pool._pins.get(id(case.owner_agent), 0)


@pytest.mark.parametrize('action', ['tasks.cancel', 'executions.cancel'])
def test_scoped_owner_control_runs_on_pinned_complete_host_and_releases(scoped_gateway, monkeypatch, action):
    from agent_py_agent.agent.gateway_parts import session_task_stop

    case = _running_scoped_task(scoped_gateway)
    try:
        observed = []
        original_stop = session_task_stop.stop_business_session_task

        def observe_stop(agent, task, **options):
            # 完整宿主就是原池里这一代准确owner实例，且在整个控制期间持有租用。
            observed.append((agent is case.owner_agent, case.pool._pins.get(id(agent), 0)))
            return original_stop(agent, task, **options)

        monkeypatch.setattr(session_task_stop, 'stop_business_session_task', observe_stop)
        key = 'r9-control-' + action.replace('.', '-')
        status, pending = _call_control(scoped_gateway, case, action, key)
        assert status == 200, pending
        assert pending['error'] is None and pending['data']['state'] == 'pending'
        assert observed == [(True, 1)]
        assert _pins(case) == 0, 'control lease must be released after the call'
        # 原运行权威已被关闭；passive宿主做不到这一步，只会永久pending。
        assert case.repo.get_agent_run(case.authority['agent_run_id'])['status'] == 'cancelled'
        control = case.store.session_tasks.require(case.task_id).metadata['cancellation_control']
        assert control['execution_ref']['observed_attempt_id'] == case.authority['attempt_id']
        assert not case.model.returned.is_set(), 'logical cancel must not report physical exit'
        _finish(case)
        status, done = _call_control(scoped_gateway, case, action, key)
        assert status == 200 and done['data']['state'] == 'completed', done
        assert _pins(case) == 0
    finally:
        case.model.release.set()
        case.thread.join(timeout=15)


def test_passive_running_detail_reads_observation_runtime_without_owner_host(scoped_gateway, monkeypatch):
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    case = _running_scoped_task(scoped_gateway)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError('passive read must not acquire or construct an owner Agent')

        for name in ('get', 'acquire_pinned', '_build_once'):
            monkeypatch.setattr(OwnerScopedAgentPool, name, forbidden)
        status, fetched = background_call(scoped_gateway, 'background_agents.get', {'agent_id': case.view['agent_id']})
        assert status == 200, fetched
        current = fetched['data']['current_execution']
        assert current is not None and current['status'] == 'running'
        assert (current['run_id'], current['attempt_id']) == (case.authority['run_id'], case.authority['attempt_id'])
        assert current['task_id'] == case.task_id
        assert _pins(case) == 0
    finally:
        _finish(case)


def test_failed_control_and_post_lease_revocation_release_pin_without_bookkeeping(scoped_gateway, monkeypatch):
    from agent_py_agent.agent.conversation import background_agents
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    case = _running_scoped_task(scoped_gateway)
    try:
        before = case.store.session_tasks.require(case.task_id).to_dict()
        run = dict(case.repo.get_agent_run(case.authority['agent_run_id']))

        def failing_cancel(host, service, params, *, actor):
            assert host is case.owner_agent and case.pool._pins.get(id(host), 0) == 1
            raise OSError('simulated I/O error inside the original control')

        monkeypatch.setattr(background_agents, 'cancel_business_execution', failing_cancel)
        status, failed = _call_control(scoped_gateway, case, 'executions.cancel', 'r9-control-failure')
        assert status == 503 and failed['error']['code'] == 'OUTCOME_UNKNOWN', failed
        assert _pins(case) == 0, 'exception path must release the control lease'
        monkeypatch.undo()

        # 构造/租用期间授权变化：同一原live check在任何账本变更前拒绝，并释放刚取得的租用。
        revoked, original_owner, original_host = [], control_service.resolve_gateway_scope_owner, api.owner_complete_host

        def drifting_owner(base, scope):
            owner = original_owner(base, scope)
            return OwnerIdentity.provider_user('feishu', 'someone-else') if revoked else owner

        def leased_then_revoked(server, owner, stack):
            host = original_host(server, owner, stack)
            assert case.pool._pins.get(id(host), 0) == 1
            revoked.append(owner)
            return host

        monkeypatch.setattr(control_service, 'resolve_gateway_scope_owner', drifting_owner)
        monkeypatch.setattr(api, 'owner_complete_host', leased_then_revoked)
        status, denied = _call_control(scoped_gateway, case, 'tasks.cancel', 'r9-control-revoked')
        assert revoked and status == 403 and denied['error']['code'] == 'CAPABILITY_DENIED', denied
        assert denied['effect_outcome'] == 'not_started'
        assert _pins(case) == 0
        assert case.store.session_tasks.require(case.task_id).to_dict() == before
        assert dict(case.repo.get_agent_run(case.authority['agent_run_id'])) == run
    finally:
        _finish(case)


def test_registered_users_keep_original_ordinary_pool_rejection(scoped_gateway):
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    _, base = scoped_gateway
    LocalUserLifecycle(LocalUserStore(base.home_paths.root)).create('alice', idempotency_key='r9-control-alice')
    with pytest.raises(HostAPIError) as refused:
        with _owner_pool(base).acquire_pinned(OwnerIdentity('local', 'user', 'alice')):
            pass
    assert refused.value.code == 'HOST_NOT_READY'
    assert not _owner_pool(base)._pins


# LLM: 保留原HTTP、限流器及503/无账本副作用断言；清桶只隔开arrange与实际控制请求。
# 函数用途: 验证多进程协调器在任何业务写入前按HOST_NOT_READY拒绝控制。
def test_countn_coordinator_control_is_not_ready_before_any_bookkeeping(production_host, reset_business_limiter):
    from agent_py_agent.agent.plugin_host.business_actions import gateway_business_port
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import plugin_observation_task
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

    services, task = plugin_observation_task(production_host)
    server = production_host[0]
    store = services.host.conversation_store
    before = (store.session_tasks.require(task.task_id).to_dict(), store.wakes.pending(limit=0))
    # 多进程协调器：只有原控制面，没有业务Agent；读口仍冷可用，W2控制在接线和调用两侧都不可用。
    server.control_plane, server.agent = SimpleNamespace(config=server.agent.config, home_paths=server.agent.home_paths,
                                                         worker_count=2), None
    reset_business_limiter(server)
    status, denied = http_request(server, production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'business.tasks.cancel', 'idempotency_key': 'r9-countn-cancel',
        'payload': {'task_id': task.task_id, 'include_descendants': True, 'expected_revision': 0}})
    assert status == 503 and denied['error']['code'] == 'HOST_NOT_READY', denied
    assert denied['error']['outcome'] == 'not_started'
    assert (store.session_tasks.require(task.task_id).to_dict(), store.wakes.pending(limit=0)) == before
    # 冷读仍经同一上下文进入原领域，并按新接线事实重建本owner服务。
    status, listed = http_request(server, production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'business.spaces.list', 'payload': {}})
    assert status == 200 and listed['ok'], listed
    backend = gateway_business_port(server.plugin_host.composition.ports.conversation)
    assert backend.ready('business.tasks.cancel') is False
    assert backend.ready('business.executions.cancel') is False
    assert backend.ready('business.background_agents.get') is True
    [current] = [value for value in backend._services.values() if value.owner_id == services.owner_id]
    assert current.complete_host_wired is False and current.ready('tasks.cancel') is False
    assert current.ready('background_agents.get') is True


def test_native_child_guidance_and_plugin_async_exclusions_are_preserved(scoped_gateway):
    from agent_py_agent.agent.plugin_host.business_actions import GatewayBusinessDomain

    status, capabilities = background_call(scoped_gateway, 'system.capabilities')
    assert status == 200, capabilities
    supported = set(capabilities['data']['supported_actions'])
    assert {'subagents.messages.send', 'subagents.cancel', 'executions.cancel', 'tasks.cancel'} <= supported
    domain = GatewayBusinessDomain(SimpleNamespace(root=scoped_gateway[1].home_paths.root, server=scoped_gateway[0]),
                                   None)
    assert domain.ready('business.subagents.messages.send') is False
    assert domain.ready('business.background_agents.resume') is False
    assert domain.ready('business.subagents.cancel') is True


@pytest.mark.parametrize("run_status,attempt_status,current_attempt,accepted", [
    ("created", "running", "attempt", True),
    ("running", "running", "attempt", True),
    ("completed", "running", "attempt", False),
    ("cancelled", "running", "attempt", False),
    ("unknown", "running", "attempt", False),
    ("created", "created", "attempt", False),
    ("created", "completed", "attempt", False),
    ("created", "unknown", "attempt", False),
    ("created", "running", "new-attempt", False),
])
def test_execution_detail_requires_live_current_attempt(run_status, attempt_status, current_attempt, accepted):
    """纯验证边界；真实只读 HTTP 往返由本文件的 passive_running_detail 用例覆盖。"""
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.background_execution_view import (
        validate_execution_context,
    )
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext

    context = BusinessExecutionContext(
        owner_id="owner", task_id="task", space_id="space", project_ref=None, space_revision=0,
        binding_id=None, selected_method_refs=(), asset_version_refs=(), schema_version="business-context.v1")
    thread = SimpleNamespace(thread_id="thread", owner_id="owner", metadata={})
    authority = {"agent_run_id": "agent-run", "attempt_id": "attempt", "task_id": "task",
                 "invocation_run_id": "request"}
    ref = SimpleNamespace(agent_run_id="agent-run", observed_attempt_id="attempt", task_run_id="task-run")
    task = SimpleNamespace(target_thread_id="thread", status="running", task_run_id="task-run",
                           conversation_request_id="request", metadata={"business_context": context.to_dict()})
    runtime = SimpleNamespace(
        get_agent_run=lambda _id: {"status": run_status, "current_attempt_id": current_attempt},
        get_attempt=lambda _id: {"status": attempt_status},
        get_task_run=lambda _id: {"task_id": "task"},
    )
    service = BackgroundAgentService(SimpleNamespace(
        conversation_store=SimpleNamespace(session_tasks=SimpleNamespace(require=lambda _id: task))))
    if accepted:
        validate_execution_context(service.store, thread, authority, ref, context, runtime=runtime)
    else:
        with pytest.raises(ValueError, match="EXECUTION_AUTHORITY_UNAVAILABLE"):
            validate_execution_context(service.store, thread, authority, ref, context, runtime=runtime)


def test_business_delivery_directory_reaches_native_model_request(scoped_gateway, monkeypatch):
    prompts = []
    original = _BlockingModel.generate

    def capture(self, prompt, *args, **kwargs):
        prompts.append(str(prompt) + repr(kwargs.get('messages', [])))
        return original(self, prompt, *args, **kwargs)

    monkeypatch.setattr(_BlockingModel, 'generate', capture)
    case = _running_scoped_task(scoped_gateway)
    try:
        assert prompts
        assert '本任务已声明交付目录:' in prompts[0]
    finally:
        _finish(case)
    assert case.store.session_tasks.require(case.task_id).status == 'done'


def test_done_session_task_still_cancels_its_live_original_attempt(scoped_gateway):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    case = _running_scoped_task(scoped_gateway)
    try:
        case.store.session_tasks.advance(case.task_id, SessionTaskUpdate('done'))
        status, pending = _call_control(scoped_gateway, case, 'tasks.cancel', 'done-task-active-attempt')
        assert status == 200, pending
        assert pending['data']['state'] == 'pending'
        assert pending['data']['outstanding_refs']
        assert case.store.session_tasks.require(case.task_id).status == 'done'
        assert case.repo.get_agent_run(case.authority['agent_run_id'])['status'] == 'cancelled'
        assert not case.model.returned.is_set()  # 控制受理不冒充原worker已退出。
    finally:
        _finish(case)


def test_closed_task_ignores_verified_other_order_claim_and_keeps_unknown_pending():
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.conversation.session_messaging import _unbound_task_run_proven_closed

    context = BusinessExecutionContext('local/main', 'space-own', None, 'order-own', 1, None, (), (), 'business-execution-context.v1')
    other = BusinessExecutionContext('local/main', 'space-other', None, 'order-other', 1, None, (), (), 'business-execution-context.v1')
    task = SimpleNamespace(task_id='order-own', task_run_id='own-run', target_thread_id='shared-controller',
        metadata={'business_context': context.to_dict()})
    claim = {'schema_version': 'background_run_claim.v1', 'claim_scope_id': 'shared-controller',
        'status': 'running', 'thread_id': 'shared-controller',
        'runtime_authority': {'schema_version': 'conversation_claim_execution.v1', 'run_id': 'other', 'attempt_id': 'other-attempt', 'agent_run_id': 'other-agent'}}
    ref = SimpleNamespace(task_run_id='other-run', run_id='other', observed_attempt_id='other-attempt', agent_run_id='other-agent')
    repo = SimpleNamespace(get_task_run=lambda _: {'task_id': 'order-own', 'closed_at': 1.0, 'status': 'done'},
        agent_runs_for_task_run=lambda _: [{'status': 'done'}], parent_execution_ref=lambda **_: ref,
        business_context_for_execution=lambda **_: other)
    host = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo),
        conversation_store=SimpleNamespace(claims=SimpleNamespace(load_report=lambda _: (claim, None))))
    assert _unbound_task_run_proven_closed(host, task, allow_other_claim=True)
    ref.task_run_id = 'own-run'
    assert not _unbound_task_run_proven_closed(host, task, allow_other_claim=True)
    ref.task_run_id = 'other-run'
    host.conversation_store.claims.load_report = lambda _: (claim, {'category': 'data_corruption'})
    assert not _unbound_task_run_proven_closed(host, task, allow_other_claim=True)
