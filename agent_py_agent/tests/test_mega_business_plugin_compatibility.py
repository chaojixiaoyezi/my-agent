"""W13生产入口合同；不把静态schema/替身当成跨片验收。"""
import pytest

from agent_py_agent.agent.plugin_host import composition
from agent_py_agent.agent.plugin_host.authorization import _requires_execution
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
from agent_py_agent.agent.plugin_host.contracts import Call, HostDomainPorts
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    UNREADY_BUSINESS_ACTIONS,
    production_call,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)


@pytest.mark.parametrize("spec", list(business_action_specs().values()), ids=lambda s: s.action)
def test_all_business_actions_select_explicit_business_domain(spec):
    assert composition.domain_name(spec.plugin_action) == "business"
    assert not composition.domain_ready(spec.plugin_action, HostDomainPorts())


@pytest.mark.parametrize("spec", list(business_action_specs().values()), ids=lambda s: s.action)
def test_business_execute_uses_original_restricted_execution_gate(spec):
    call = Call("plugin_host_call.v2", spec.plugin_action, {})
    assert _requires_execution(call) is (spec.metadata["effect"] == "execute")


def test_business_stream_is_transport_only_and_legacy_tasks_unchanged():
    from agent_py_agent.agent.plugin_host.action_specs import action_spec
    assert composition.domain_name("business.events.stream") == "business"
    assert action_spec("business.events.stream").mode == "stream"
    assert composition.domain_name("tasks.list") == "observations"
    assert action_spec("tasks.list").capabilities == {"tasks.read"}


def test_new_business_caps_do_not_gift_old_execution_or_human_gate():
    from agent_py_agent.agent.plugin_host.action_specs import required_capabilities
    for spec in business_action_specs().values():
        required = required_capabilities(spec.plugin_action, {})
        if spec.metadata['effect'] == 'execute':
            assert 'execute' in required
    assert 'interactions.respond' in required_capabilities('business.approvals.decide', {})


def business_heartbeat():
    return {'stream_item': 'heartbeat', 'event_id': 'event-one', 'cursor': 'opaque-cursor',
        'event_type': 'heartbeat', 'occurred_at': '2026-11-01T12:00:00Z', 'node': None,
        'space_id': None, 'task_id': None, 'run_id': None, 'attempt_id': None, 'operation_id': None,
        'payload': {'server_time': '2026-11-01T12:00:00Z'}, 'coverage': 'unknown'}


def test_business_typed_event_uses_original_bounded_sse_pump(monkeypatch):
    """运输叶子替身；W14真人scope/续传联测仍须独立验收。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host import http_transport as transport
    event = business_heartbeat()
    monkeypatch.setattr(transport, 'stream_events', lambda *args: (row for row in [event]))
    authorized = SimpleNamespace(call=SimpleNamespace(action='business.events.stream'))
    session = transport.StreamSession(authorized, HostDomainPorts())
    session.ready.set()
    session.produce()
    assert session.events.get_nowait() == ('business', event)
    assert session.events.get_nowait() is None
    assert session.events.maxsize == 8
    assert (transport.STREAM_MAX_COUNT, transport.INSTALL_STREAM_MAX_COUNT, transport.HEARTBEAT_SECONDS) == (16, 2, 15)


def test_business_approval_normalization_is_reissued_by_original_issuer(production_host):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host import authorization
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    server, token, *_ = production_host
    issuer = server.plugin_host.composition.issuer
    original = issuer.authorize(issuer.ingress(token), decode_call(json.dumps({'schema': 'plugin_host_call.v2',
        'action': 'business.approvals.decide', 'payload': {'approval_id': 'approval-one', 'decision': 'approved',
            'expected_revision': 1, 'response_ticket': 'opaque-ticket'}, 'idempotency_key': 'business-approval-key'}).encode()))
    normalize = getattr(authorization, 'canonical_business_approval', None)
    assert callable(normalize)
    canonical = normalize(original)
    authorization.revalidate_call(canonical)
    assert canonical is not original and canonical.call.action == 'interactions.approvals.respond'
    assert canonical.call.payload['interaction_id'] == 'approval-one'
    assert canonical.effective_owner == original.effective_owner
    with pytest.raises(HostAPIError):
        normalize(replace(original))


def test_actual_business_plugin_uses_shared_space_service(production_host):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
    server, _, owner, *_ = production_host
    server.agent = SimpleNamespace(config=AgentConfig(business_spaces_enabled=True), home_paths=home_paths_with_owner(home_paths(owner.root), owner))
    created = production_call(production_host, 'business.spaces.create', {'name': '插件空间'}, key='plugin-business-create')
    fetched = production_call(production_host, 'business.spaces.get', {'space_id': created['space_id']})
    assert fetched['space']['name'] == '插件空间'
    assert 'spaces.create' in production_call(production_host, 'business.system.capabilities')['supported_actions']


# LLM: 冷/热接线同守准确九项执行不可用，保留全目录与无副作用验证，不能将声明目录当完整执行支持。
# 函数用途: 验证读取/控制接线不依赖暖Agent且执行动作始终关闭。
@pytest.mark.parametrize('enabled', [None, True, False], ids=['cold-gateway', 'bound-enabled', 'bound-disabled'])
def test_business_host_wiring_does_not_require_a_warm_agent(production_host, enabled):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host.business_action_specs import business_plugin_actions
    server, _, owner, *_ = production_host
    if enabled is not None:
        server.agent = SimpleNamespace(config=SimpleNamespace(business_spaces_enabled=enabled))
    backend = composition.business_port(server.plugin_host.composition.ports)
    before = sorted(path.relative_to(owner.root) for path in owner.root.rglob('*'))
    readiness = {action: backend.ready(action) for action in business_plugin_actions()}
    assert readiness == {action: enabled is not False and action not in UNREADY_BUSINESS_ACTIONS
                         for action in business_plugin_actions()}
    assert not backend.ready('business.unregistered.action')
    assert backend._services == {}
    assert sorted(path.relative_to(owner.root) for path in owner.root.rglob('*')) == before
    if enabled is None:
        assert server.agent is None


def test_business_host_wiring_requires_original_gateway_root(production_host, monkeypatch):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_plugin_actions
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    monkeypatch.setattr(backend.resources, 'root', None)
    assert not any(backend.ready(action) for action in business_plugin_actions())


def plugin_observation_task(production_host, *, user=None):
    """原安装/grant/issuer及HTTP创建空间，任务和运行事件只准备在原规范库。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    server, _, owner, *_ = production_host
    if server.agent is None:
        server.agent = SimpleNamespace(config=AgentConfig(business_spaces_enabled=True),
            home_paths=home_paths_with_owner(home_paths(owner.root), owner))
    backend = composition.business_port(server.plugin_host.composition.ports)
    before = set(backend._services)
    space = production_call(production_host, 'business.spaces.create', {'name': '观察空间'},
        user=user, key='observation-space-' + (user or 'main'))
    [services] = [value for key, value in backend._services.items() if key not in before]
    store = services.host.conversation_store
    source = store.threads.require(production_call(production_host, 'conversations.threads.create',
        {'title': '原安装来源'}, user=user, key='observation-source-thread', status=201)['thread_id'])
    target = store.threads.require(production_call(production_host, 'conversations.threads.create',
        {'title': '原安装目标'}, user=user, key='observation-target-thread', status=201)['thread_id'])
    intent = BusinessContextIntent(services.owner_id, space['space_id'], None, None)
    task = store.session_tasks.create(SessionTaskDraft(source.thread_id, target.thread_id, '插件原任务'),
        business_context_intent=intent, business_context_factory=lambda tid: BusinessExecutionContext(
            services.owner_id, space['space_id'], None, tid, 0, None, (), (), 'business-context.v1'))
    return services, task


def open_plugin_observation(production_host, *, cursor=None):
    import http.client
    import json

    server, token, *_ = production_host
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    conn.request('POST', '/plugin-host/v2/stream', json.dumps({'schema': 'plugin_host_call.v2',
        'action': 'business.events.stream', 'payload': {'request_id': 'plugin-observation', 'filter': {},
            **({'cursor': cursor} if cursor else {})}}).encode(),
        {'Content-Type': 'application/json', 'X-Plugin-Host-Token': token})
    return conn, conn.getresponse()


def read_business_sse(response):
    import json

    name, data = None, None
    for _ in range(32):
        line = response.readline()
        if not line:
            return None, None
        if line.startswith(b'event: '):
            name = line[7:].strip().decode()
        if line.startswith(b'data: '):
            data = json.loads(line[6:])
        if line == b'\n' and name is not None:
            return name, data
    pytest.fail('没有读到完整有界SSE帧')


def test_observation_plugin_query_uses_owner_service_and_null_workflow(production_host):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    services, task = plugin_observation_task(production_host)
    db = runtime_db_path(services.owner_home)
    assert not db.exists()
    listed = production_call(production_host, 'business.tasks.list')
    assert [row['task_id'] for row in listed['items']] == [task.task_id]
    detail = production_call(production_host, 'business.tasks.get', {'task_id': task.task_id})
    assert detail['workflow_ref'] is None and detail['assignments'][0]['task_id'] == task.task_id
    assert not db.exists()
    production_call(production_host, 'users.local.create', {'user_id': 'alice'}, key='observe-create-alice', status=201)
    alice, alice_task = plugin_observation_task(production_host, user='alice')
    assert alice.observation is not services.observation
    assert [row['task_id'] for row in production_call(production_host, 'business.tasks.list', user='alice')['items']] == [alice_task.task_id]
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    status, denied = http_request(production_host[0], production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'business.tasks.get', 'as_user': {'user_id': 'alice'}, 'payload': {'task_id': task.task_id}})
    assert status == 404 and denied['error']['code'] == 'OBSERVATION_NOT_FOUND'


@pytest.mark.parametrize('burst', [1, 9])
def test_observation_plugin_real_sse_and_request_local_eight_event_bound(production_host, burst):
    from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
    from agent_py_agent.agent.gateway_parts.business_event_stream import stream_business_events
    from agent_py_agent.agent.plugin_host.business_action_specs import standalone_schema
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime

    services, task = plugin_observation_task(production_host)
    runtime, run = seed_observed_runtime(services, task)
    events = [runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
        agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id']) for _ in range(burst)]
    conn, response = open_plugin_observation(production_host)
    try:
        assert response.status == 200, response.read()
        assert response.getheader('Content-Type').startswith('text/event-stream')
        name, frame = read_business_sse(response)
        assert name == 'business' and isinstance(frame, dict)
        assert frame['stream_item'] == 'snapshot' and frame['payload']['tasks'][0]['task_id'] == task.task_id
        validate_object(frame, standalone_schema({'$ref': '#/$defs/EventView'}))
        for _ in range(8):
            name, frame = read_business_sse(response)
            assert name == 'business', frame
            validate_object(frame, standalone_schema({'$ref': '#/$defs/EventView'}))
            if frame['stream_item'] == 'gap' or frame['event_id'] == events[-1]['event_id']:
                break
        else:
            pytest.fail('真实业务SSE没有交付事件/背压缺口')
        if burst == 9:
            assert frame['stream_item'] == 'gap' and frame['payload']['reset_required']
        else:
            assert frame['event_id'] == events[-1]['event_id'] and frame['attempt_id'] == run['attempt_id']
    finally:
        response.close()
        conn.close()
    assert not hasattr(services, 'stream_transport'), 'Plugin不能改变共享原生服务的缓冲配置'
    context = bind_business_context(services.host, authorize=lambda _a, _f: True)
    native = stream_business_events({'request_id': 'native-after-plugin', 'filter': {}},
        authenticated_context=context, services=services)
    assert native.max_pending == 128
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status


def test_observation_plugin_sse_survives_short_runtime_write_lock(production_host, monkeypatch):
    """在200头之后制造真实WAL独占窗口，仍须交付原snapshot，不能静默EOF。"""
    import threading
    import traceback
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.plugin_host.http_transport import StreamSession
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime

    services, task = plugin_observation_task(production_host)
    runtime, run = seed_observed_runtime(services, task)
    locked, reading, release = threading.Event(), threading.Event(), threading.Event()
    original_connect = runtime._runtime_connect
    original_revalidate = StreamSession._revalidate_stream
    checks, faults, published = 0, [], []

    def writer_connect():
        conn = original_connect()
        try:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            conn.execute('PRAGMA locking_mode=EXCLUSIVE')
            conn.execute('BEGIN EXCLUSIVE')
            locked.set()
            assert reading.wait(5), 'SSE没有进入锁竞争读入口'
            # 被测的是零等待与短竞争的差别：读入口到达后保留100ms物理锁，
            # 不是用sleep猜测请求何时完成；finally另有显式释放与线程回收。
            release.wait(0.1)
            return conn
        except BaseException:
            conn.close()
            raise

    with ThreadPoolExecutor(max_workers=1) as pool:
        def revalidate(session):
            nonlocal checks
            checks += 1
            if checks == 2:
                published.append(pool.submit(runtime.append_event, event_type='attempt.progress',
                    attempt_id=run['attempt_id'], agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id']))
                assert locked.wait(5), '写者没有取得真实SQLite锁'
                reading.set()
            try:
                return original_revalidate(session)
            except Exception as error:
                faults.append((type(error).__name__, getattr(error, 'sqlite_errorcode', None), traceback.format_exc()))
                raise

        monkeypatch.setattr(runtime, '_runtime_connect', writer_connect)
        monkeypatch.setattr(StreamSession, '_revalidate_stream', revalidate)
        conn, response = open_plugin_observation(production_host)
        try:
            assert response.status == 200, response.read()
            name, frame = read_business_sse(response)
            assert name == 'business' and isinstance(frame, dict), '\n'.join(fault[2] for fault in faults)
            assert frame['stream_item'] == 'snapshot'
            assert [row['task_id'] for row in frame['payload']['tasks']] == [task.task_id]
            assert checks >= 2 and locked.is_set() and reading.is_set()
            assert not faults
            assert published[0].result(timeout=5)['attempt_id'] == run['attempt_id']
        finally:
            release.set()
            response.close()
            conn.close()


def test_observation_runtime_reader_wait_budget_preserves_read_only_and_c_short_writes(production_host):
    import sqlite3

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime

    services, task = plugin_observation_task(production_host)
    runtime, _ = seed_observed_runtime(services, task)
    with services.observation.runtime._runtime_connection() as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
        with pytest.raises(sqlite3.OperationalError) as denied:
            conn.execute("INSERT INTO metadata(key,value) VALUES ('observation-write','forbidden')")
        assert denied.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    # 观察读口的等待不能扩散到C现存库短提交，也不能把缺库创建成空事实源。
    coordinator = RuntimeRepository(runtime.db_path, existing_only=True)
    with coordinator._runtime_connection() as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 0
    missing = runtime.db_path.parent / 'missing-observation-runtime.db'
    reader = RuntimeRepository(missing, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        with reader._runtime_connection():
            pytest.fail('只读连接不应创建缺库')
    assert not missing.exists()


def scoped_authorized(host, action, payload=None, *, user=None, key=None):
    import json

    from agent_py_agent.agent.plugin_host.contracts import decode_call

    request = {'schema': 'plugin_host_call.v2', 'action': action, 'payload': payload or {}}
    if user:
        request['as_user'] = {'user_id': user}
    if key:
        request['idempotency_key'] = key
    issuer = host[0].plugin_host.composition.issuer
    return issuer.authorize(issuer.ingress(host[1]), decode_call(json.dumps(request).encode()))


def native_scope_rows(host, *, user=None):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.gateway_parts.business_api import (
        _BusinessBackgroundService,
        bind_business_context,
    )

    if user:
        production_call(host, 'users.local.create', {'user_id': user}, key='scope-create-' + user, status=201)
    services, allowed = plugin_observation_task(host, user=user)
    store = services.host.conversation_store
    source = store.threads.create(owner_id=services.owner_id, owner_home=str(services.owner_home))
    actor = HostChannelActor(services.owner_id, source.thread_id, 'native-original-request',
        'feishu', 'native-chat', 'native-human')
    context = bind_business_context(services.host, authorize=lambda _a, _f: True, actor=actor)
    native = _BusinessBackgroundService(services, context)
    # 真实原生服务创建，不给这些记录贴插件来源，也不复制已发行AuthorizedCall。
    services.host.config.model_backend = 'echo'
    controller = native.ensure_controller(actor=actor)['thread_id']
    space = allowed.metadata['business_context']['space_id']
    private_agent = native.create({'space_id': space, 'name': '原生私有后台身份'},
        actor=actor, idempotency_key='native-private-agent')['agent']['thread_id']
    private = store.session_tasks.create(SessionTaskDraft(source.thread_id, private_agent, '原生私有任务'),
        business_context_intent=BusinessContextIntent(services.owner_id, space, None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, space,
            None, tid, 0, None, (), (), 'business-context.v1'))
    store.messages.append({'thread_id': controller, 'role': 'assistant', 'content': 'NATIVE_PRIVATE_MESSAGE'})
    return SimpleNamespace(services=services, allowed=allowed, private=private,
        controller=controller, agent=private_agent, native=native, actor=actor, space=space)


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'alice'])
def test_scope_installation_only_denies_private_controller_on_legacy_and_business(production_host, user):
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

    case = native_scope_rows(production_host, user=user)
    for action, payload in [('conversations.messages.list', {'thread_id': case.controller}),
                            ('business.messages.list', {'target': {'kind': 'controller', 'id': case.controller}})]:
        request = {'schema': 'plugin_host_call.v2', 'action': action, 'payload': payload}
        if user:
            request['as_user'] = {'user_id': user}
        status, result = http_request(production_host[0], production_host[1], request)
        assert status in {403, 404}, (action, result)
        assert 'NATIVE_PRIVATE_MESSAGE' not in str(result)


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'alice'])
def test_scope_mixed_lists_tree_runtime_and_stream_keep_only_original_installation(production_host, user):
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime

    case = native_scope_rows(production_host, user=user)
    runtime, permitted_run = seed_observed_runtime(case.services, case.allowed)
    _, private_run = seed_observed_runtime(case.services, case.private)
    permitted_event = runtime.append_event(event_type='attempt.progress', attempt_id=permitted_run['attempt_id'],
        agent_run_id=permitted_run['agent_run_id'], task_run_id=permitted_run['task_run_id'])
    private_event = runtime.append_event(event_type='attempt.progress', attempt_id=private_run['attempt_id'],
        agent_run_id=private_run['agent_run_id'], task_run_id=private_run['task_run_id'])
    listed = production_call(production_host, 'business.tasks.list', user=user)
    assert [row['task_id'] for row in listed['items']] == [case.allowed.task_id]
    executions = production_call(production_host, 'business.executions.list', user=user)
    assert [row['run_id'] for row in executions['items']] == ['observed-' + case.allowed.task_id]
    tree = production_call(production_host, 'business.agent_tree.get', {'space_id': case.space}, user=user)
    assert case.agent not in str(tree) and case.controller not in str(tree)
    events = production_call(production_host, 'business.events.list', user=user)
    assert {row['task_id'] for row in events['events']} == {case.allowed.task_id}
    assert permitted_event['event_id'] in {row['event_id'] for row in events['events']}
    assert private_event['event_id'] not in {row['event_id'] for row in events['events']}
    authorized = scoped_authorized(production_host, 'business.events.stream',
        {'request_id': 'scope-stream', 'filter': {}}, user=user)
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    iterator = backend.stream(authorized)
    try:
        snapshot = next(iterator)
        assert [row['task_id'] for row in snapshot['payload']['tasks']] == [case.allowed.task_id]
        assert case.private.task_id not in str(snapshot)
    finally:
        iterator.close()


def test_scope_plugin_actor_is_original_and_factory_read_discovery_creates_no_thread(production_host):
    from agent_py_agent.agent.plugin_host.authorization import revalidate_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    services, _ = plugin_observation_task(production_host)
    authorized = scoped_authorized(production_host, 'business.background_agents.list')
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    context, _ = backend.context(authorized)
    assert context.actor is authorized
    revalidate_call(context.actor)
    assert callable(context.actor_factory)
    before = services.host.conversation_store.threads.list_report(limit=0)
    with pytest.raises(HostAPIError) as denied:
        backend.call(authorized)
    assert denied.value.status == 404
    assert services.host.conversation_store.threads.list_report(limit=0) == before


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'alice'])
def test_scope_plugin_background_create_replay_and_controller_keep_validated_provenance(production_host, user):
    if user:
        production_call(production_host, 'users.local.create', {'user_id': user}, key='actor-create-' + user, status=201)
    services, task = plugin_observation_task(production_host, user=user)
    space = task.metadata['business_context']['space_id']
    params = {'space_id': space, 'name': '插件合法身份'}
    first = production_call(production_host, 'business.background_agents.create', params,
        user=user, key='plugin-scope-background-create')
    before_replay = services.host.conversation_store.threads.list_report(limit=0)
    replay = production_call(production_host, 'business.background_agents.create', params,
        user=user, key='plugin-scope-background-create')
    assert first == replay
    assert services.host.conversation_store.threads.list_report(limit=0) == before_replay
    agent = production_call(production_host, 'business.background_agents.get',
        {'agent_id': first['agent']['agent_id']}, user=user)
    assert agent['agent']['name'] == params['name']
    controller = production_call(production_host, 'business.controllers.ensure', user=user, key='plugin-scope-controller')
    assert production_call(production_host, 'business.controllers.get', user=user)['thread_id'] == controller['thread_id']
    authorized = scoped_authorized(production_host, 'business.background_agents.get',
        {'agent_id': first['agent']['agent_id']}, user=user)
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    context, _ = backend.context(authorized)
    actor = context.actor_factory('background_agents.get')
    for tid in (first['agent']['thread_id'], controller['thread_id'], actor.source_thread_id):
        row = services.host.conversation_store.threads.require(tid)
        assert row.owner_id == services.owner_id and row.owner_home == str(services.owner_home)
        assert row.metadata['plugin_host_v2']['installation_ref'] == authorized.binding.installation_ref
    assert actor.owner_id == services.owner_id and actor.channel == 'plugin'
    assert actor.conversation_id == authorized.binding.installation_ref
    assert context.actor is authorized


@pytest.fixture
def explicit_scope_host(monkeypatch, request):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.tests import test_plugin_host_v2_grants as grants

    original = grants.installed_grant

    def installed(*args, **kwargs):
        result = original(*args, **kwargs)
        controller = BackgroundAgentService._controller_id(None, SimpleNamespace(owner_id=result[0].owner_id))
        result[-1]['threads']['explicit_ids'] = ['thread-explicit-native', controller]
        return result

    monkeypatch.setattr(grants, 'installed_grant', installed)
    return request.getfixturevalue('production_host')


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'alice'])
def test_scope_explicit_native_and_original_installation_messages_are_usable(explicit_scope_host, user):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic

    host = explicit_scope_host
    if user:
        production_call(host, 'users.local.create', {'user_id': user}, key='explicit-create-alice', status=201)
    services, task = plugin_observation_task(host, user=user)
    store = services.host.conversation_store
    native = create_thread_atomic(store.threads, ConversationThread(thread_id='thread-explicit-native',
        canonical_user_id=services.owner_id, owner_id=services.owner_id,
        owner_home=str(services.owner_home), title='明确授权的原生线程', metadata={'business_controller': True}))
    for tid in (native.thread_id, task.target_thread_id):
        row = store.threads.require(tid)
        store.threads.write(replace(row, metadata={**row.metadata, 'business_controller': True}))
        store.messages.append({'thread_id': tid, 'role': 'assistant', 'content': 'PERMITTED_' + tid})
        legacy = production_call(host, 'conversations.messages.list', {'thread_id': tid}, user=user)
        business = production_call(host, 'business.messages.list',
            {'target': {'kind': 'controller', 'id': tid}}, user=user)
        assert 'PERMITTED_' + tid in str(legacy)
        assert 'PERMITTED_' + tid in str(business)


def revoke_business_installation(host):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore

    store = PluginInstallStore(host[2])
    entry = next(row for row in store.snapshot() if row.installation_ref == host[3].installation_ref)
    store.change_activation(PluginActivationRequest('revoke-business-test', entry.revision,
        replace(entry.activation, phase='revoked')))


@pytest.mark.parametrize('stage', ['before', 'final-response', 'origin-creation', 'stream-event'])
def test_scope_live_revocation_stops_disclosure_and_origin_effect(production_host, monkeypatch, stage):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    services, task = plugin_observation_task(production_host)
    backend = composition.business_port(production_host[0].plugin_host.composition.ports)
    store = services.host.conversation_store
    if stage == 'stream-event':
        authorized = scoped_authorized(production_host, 'business.events.stream',
            {'request_id': 'revoke-stream', 'filter': {}})
        iterator = backend.stream(authorized)
        try:
            assert next(iterator)['stream_item'] == 'snapshot'
            revoke_business_installation(production_host)
            with pytest.raises(HostAPIError):
                next(iterator)
        finally:
            iterator.close()
        return
    if stage == 'origin-creation':
        authorized = scoped_authorized(production_host, 'business.background_agents.create',
            {'space_id': task.metadata['business_context']['space_id'], 'name': '不得创建'}, key='revoked-origin-create')
        original = backend.conversation.new_thread

        def revoke_during_creation(*args, **kwargs):
            value = original(*args, **kwargs)
            revoke_business_installation(production_host)
            return value

        monkeypatch.setattr(backend.conversation, 'new_thread', revoke_during_creation)
    else:
        authorized = scoped_authorized(production_host, 'business.tasks.get', {'task_id': task.task_id})
        if stage == 'before':
            revoke_business_installation(production_host)
        else:
            original = services.invoke

            def revoke_after_read(*args, **kwargs):
                value = original(*args, **kwargs)
                revoke_business_installation(production_host)
                return value

            monkeypatch.setattr(services, 'invoke', revoke_after_read)
    before = store.threads.list_report(limit=0)
    with pytest.raises(HostAPIError) as denied:
        backend.call(authorized)
    assert denied.value.status in {401, 403}
    assert store.threads.list_report(limit=0) == before


@pytest.mark.parametrize('kind', ['native', 'other-installation', 'inactive'])
def test_scope_explicit_existing_controllers_are_never_retagged_or_reactivated(explicit_scope_host, kind):
    from dataclasses import replace

    case = native_scope_rows(explicit_scope_host)
    store = case.services.host.conversation_store
    row = store.threads.require(case.controller)
    if kind == 'other-installation':
        row = replace(row, metadata={**row.metadata, 'plugin_host_v2': {
            'installation_ref': 'original-other-installation', 'revision': 1, 'workspace_ref': 'default'}})
    elif kind == 'inactive':
        row = replace(row, status='archived')
    store.threads.write(row)
    # 先用合法写动作建立本安装的真实来源，不能借现有总控作假身份。
    production_call(explicit_scope_host, 'business.background_agents.create',
        {'space_id': case.space, 'name': '合法插件身份'}, key='controller-original-source')
    before = store.storage.thread_path(case.controller).read_bytes()
    result = production_call(explicit_scope_host, 'business.controllers.ensure', key='existing-controller')
    assert result == {'agent_id': case.controller, 'thread_id': case.controller, 'created': False}
    assert production_call(explicit_scope_host, 'business.controllers.get')['available'] is (kind != 'inactive')
    assert store.storage.thread_path(case.controller).read_bytes() == before


@pytest.mark.parametrize('permitted', [False, True], ids=['private-task', 'original-task'])
def test_scope_agent_detail_filters_current_execution_using_original_task_and_run(production_host, permitted):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime

    case = native_scope_rows(production_host)
    created = production_call(production_host, 'business.background_agents.create',
        {'space_id': case.space, 'name': '可见身份的准确执行'}, key='current-execution-agent')
    agent_id = created['agent']['agent_id']
    store = case.services.host.conversation_store
    sender = case.allowed.sender_thread_id if permitted else case.actor.source_thread_id
    task = store.session_tasks.create(SessionTaskDraft(sender, agent_id, '当前执行范围检查'),
        business_context_intent=BusinessContextIntent(case.services.owner_id, case.space, None, None),
        business_context_factory=lambda tid: BusinessExecutionContext(case.services.owner_id, case.space,
            None, tid, 0, None, (), (), 'business-context.v1'))
    runtime, run = seed_observed_runtime(case.services, task)
    run_id = 'observed-' + task.task_id
    case.services.host.subagents = SimpleNamespace(runtime_db=runtime)
    claim = store.claims.acquire({'thread_id': agent_id, 'task_id': task.task_id})
    assert claim is not None
    update_json_file_atomic(store.storage.background_claim_path(agent_id), lambda latest: {
        **latest, 'runtime_authority': {'run_id': run_id, 'attempt_id': run['attempt_id']}})
    result = production_call(production_host, 'business.background_agents.get', {'agent_id': agent_id})
    if permitted:
        assert result['current_execution']['run_id'] == run_id
        assert result['current_execution']['task_id'] == task.task_id
        assert [row['task_id'] for row in result['pending_tasks']] == [task.task_id]
    else:
        assert result['current_execution'] is None
        assert result['pending_tasks'] == []
        assert task.task_id not in str(result) and run_id not in str(result)
