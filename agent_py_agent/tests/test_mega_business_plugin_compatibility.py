"""W13生产入口合同；不把静态schema/替身当成跨片验收。"""
import pytest

from agent_py_agent.agent.plugin_host import composition
from agent_py_agent.agent.plugin_host.authorization import _requires_execution
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
from agent_py_agent.agent.plugin_host.contracts import Call, HostDomainPorts
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
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
    assert readiness == dict.fromkeys(business_plugin_actions(), enabled is not False)
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
    source = store.threads.create(owner_id=services.owner_id)
    target = store.threads.create(owner_id=services.owner_id)
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
            assert name == 'business'
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
