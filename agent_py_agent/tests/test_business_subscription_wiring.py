"""订阅发现与原catalog接线；没有来源权威时不宣称已连接或可写。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.conversation.managed_subscriptions import ManagedSubscriptionService
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.cli.chat_client_context import post_gateway_json
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native


@pytest.fixture
def wired(native):
    server, agent = native
    agent.config.gateway_port = server.server.server_address[1]
    client = BusinessClient(lambda route, payload, timeout: post_gateway_json(agent, route, payload, timeout=timeout))
    catalog = BusinessSpaceService(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id, enabled=True)
    source = {'owner_id': agent.home_paths.owner_id, 'source_ref': 'source-test', 'connection_state': 'not_connected'}
    seen = []
    def resolver(ref, actor):
        seen.append((ref, actor.subject_id))
        if ref != source['source_ref']:
            raise BusinessError('REFERENCE_NOT_FOUND')
        return dict(source)
    service = ManagedSubscriptionService(catalog, source_resolver=resolver)
    return agent, client, service, source, seen


def call(client, action, params=None, **options):
    return client.send(client.prepare(action, params or {}, **options))


def test_default_lists_canonical_empty_but_never_advertises_source_writes(wired):
    agent, client, _service, _, _ = wired
    supported = call(client, 'system.capabilities')['data']['supported_actions']
    assert 'subscriptions.list' in supported
    assert not any(action in supported for action in ('subscriptions.create', 'subscriptions.get', 'subscriptions.resume'))
    listed = call(client, 'subscriptions.list')
    assert listed['ok'] and listed['data'] == {'items': [], 'next_cursor': None, 'coverage': 'complete'}
    denied = call(client, 'subscriptions.create', {'space_id': 'absent', 'source_ref': 'source-test'})
    assert denied['error']['code'] == 'HOST_NOT_READY' and denied['effect_outcome'] == 'not_started'
    assert not agent.conversation_store.session_tasks.list_report(limit=0)[0]


def test_injected_original_service_crud_cas_and_idempotency_roundtrip(wired):
    agent, client, service, _, _ = wired
    agent.business_subscriptions = lambda owner: service if owner == service.owner_id else None
    supported = call(client, 'system.capabilities')['data']['supported_actions']
    assert {f'subscriptions.{name}' for name in ('create','list','get','update','pause','resume','remove')} <= set(supported)
    sid = call(client, 'spaces.create', {'name': 'subscription-scope'})['data']['space_id']
    prepared = client.prepare('subscriptions.create', {'space_id': sid, 'source_ref': 'source-test'},
                              idempotency_key='subscription-create-exact-key')
    created = client.send(prepared)
    assert created['ok'] and client.send(prepared)['data'] == created['data']
    sub = created['data']['subscription']
    subid = sub['subscription_id']
    assert sub['connection_state'] == 'not_connected'
    assert call(client, 'subscriptions.get', {'subscription_id': subid})['data']['subscription'] == sub
    updated = call(client, 'subscriptions.update', {'subscription_id': subid, 'routing_instruction': '分类归档'}, expected_revision=0)
    assert updated['ok'] and updated['data']['revision'] == 1
    conflict = call(client, 'subscriptions.pause', {'subscription_id': subid}, expected_revision=0)
    assert conflict['error']['code'] == 'REVISION_CONFLICT' and conflict['effect_outcome'] == 'not_started'
    for action,revision,state in [('pause',1,'paused'),('resume',2,'active'),('remove',3,'removed')]:
        response = call(client, 'subscriptions.'+action, {'subscription_id': subid}, expected_revision=revision)
        assert response['ok'] and response['data']['state'] == state
    assert call(client, 'subscriptions.list')['data']['items'][0]['state'] == 'removed'
    refused = call(client, 'subscriptions.resume', {'subscription_id': subid}, expected_revision=4)
    assert refused['http_status'] == 409 and refused['error']['code'] == 'SUBSCRIPTION_REMOVED'
    assert refused['effect_outcome'] == 'not_started'
    assert not agent.conversation_store.session_tasks.list_report(limit=0)[0]


def test_source_factory_removed_does_not_reuse_cached_authority(wired):
    agent, client, service, _, _ = wired
    agent.business_subscriptions = lambda owner: service
    sid = call(client, 'spaces.create', {'name': 'revoked-source'})['data']['space_id']
    created = call(client, 'subscriptions.create', {'space_id': sid, 'source_ref': 'source-test'})
    assert created['ok']
    del agent.business_subscriptions
    supported = call(client, 'system.capabilities')['data']['supported_actions']
    assert 'subscriptions.create' not in supported
    result = call(client, 'subscriptions.list')
    assert not result['ok'] and result['error']['code'] == 'HOST_NOT_READY'
    assert result['effect_outcome'] == 'not_started'
    assert {'key':'dependency','value':'subscription_source_resolver'} in result['error']['details']
    assert result['data'] == {}


def test_owner_and_source_scope_not_bypassed_by_discovery(tmp_path):
    home = tmp_path / 'owner'
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='owner',owner_home_dir=home),
                           config=SimpleNamespace(business_spaces_enabled=True))
    catalog = BusinessSpaceService(home, owner_id='owner', enabled=True)
    allowed = [True]
    actor = BusinessActor('owner', 'subject', catalog, lambda action,facts: allowed[0])
    sid = catalog.create({'name':'source-scope'},actor=actor,idempotency_key='space-key')['space_id']
    seen = []
    def source(ref, current):
        seen.append(ref)
        return {'owner_id':'owner','source_ref':ref,'connection_state':'connected'}
    service = ManagedSubscriptionService(catalog,source_resolver=source)
    created = service.execute('subscriptions.create',{'space_id':sid,'source_ref':'private-source'},actor=actor,idempotency_key='create-key')
    seen.clear()
    services = api.BusinessServices(host,subscriptions=service)
    context = api.bind_business_context(host,authorize=lambda action,facts: not facts.get('source_ref'))
    status,result = api.execute_business({'action':'subscriptions.list','request_id':'scope-list','params':{}},context,services)
    assert status==200 and result['data']['items']==[]
    assert seen==[], '无权行不能进入source resolver'
    status,result = api.execute_business({'action':'subscriptions.get','request_id':'scope-get',
        'params':{'subscription_id':created['subscription']['subscription_id']}},context,services)
    assert status==403 and result['error']['code']=='BUSINESS_ACCESS_DENIED'
    alien = ManagedSubscriptionService(BusinessSpaceService(tmp_path/'alien',owner_id='other',enabled=True),source_resolver=source)
    assert not api.BusinessServices(host,subscriptions=alien).ready('subscriptions.list')


def test_original_subscription_pagination_and_scope_stay_authoritative(wired):
    agent, client, service, _, _ = wired
    agent.business_subscriptions = lambda owner: service
    sid = call(client, 'spaces.create', {'name': 'page-scope'})['data']['space_id']
    ids = set()
    for index in range(2):
        result = call(client, 'subscriptions.create', {'space_id': sid, 'source_ref': 'source-test'},
                      idempotency_key=f'subscription-page-{index:04}')
        assert result['ok']
        ids.add(result['data']['subscription']['subscription_id'])
    first = call(client, 'subscriptions.list', {'space_id': sid, 'limit': 1})
    assert first['data']['coverage'] == 'partial' and first['data']['next_cursor']
    second = call(client, 'subscriptions.list', {'space_id': sid, 'limit': 1, 'cursor': first['data']['next_cursor']})
    assert {first['data']['items'][0]['subscription_id'], second['data']['items'][0]['subscription_id']} == ids
    other = call(client, 'spaces.create', {'name': 'other-page-scope'})['data']['space_id']
    assert call(client, 'subscriptions.list', {'space_id': other})['data']['items'] == []


@pytest.mark.parametrize('failure', ['source_owner', 'policy'])
def test_source_and_policy_refusals_never_commit_subscription(wired, failure):
    agent, client, service, source, _ = wired
    agent.business_subscriptions = lambda owner: service
    sid = call(client, 'spaces.create', {'name': 'refusal-scope'})['data']['space_id']
    params = {'space_id': sid, 'source_ref': 'source-test'}
    if failure == 'source_owner':
        source['owner_id'] = 'unrelated-owner'
    else:
        params['policy_ref'] = 'ungranted-policy'
    result = call(client, 'subscriptions.create', params)
    expected = 'SOURCE_SCOPE_MISMATCH' if failure == 'source_owner' else 'POLICY_AUTHORIZATION_REQUIRED'
    assert result['http_status'] == 403 and result['error']['code'] == expected
    assert result['effect_outcome'] == 'not_started'
    assert service.catalog._read().get('subscriptions', {}) == {}


def _signed_native_push(native, body, secret):
    import hashlib
    import hmac
    import http.client
    import json
    import time
    import uuid

    server, _ = native
    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    stamp, nonce = str(int(time.time())), uuid.uuid4().hex
    signature = hmac.digest(secret.encode(), f'{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}'.encode(), 'sha256').hex()
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=30)
    try:
        conn.request('POST', '/remote-agent/push', raw, {'Content-Type': 'application/json',
            'X-Agent-Push-Token': secret, 'X-Agent-Timestamp': stamp, 'X-Agent-Nonce': nonce, 'X-Agent-Signature': signature})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_native_subscription_token_routes_only_explicit_bound_source_and_revokes(native, monkeypatch):
    from agent_py_agent.tests.test_business_v12_background_scheduling import (
        add_echo_profile,
        ok,
        scheduler_for,
        tick,
    )

    _, host = native
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
    wire_business_host_ports(host)
    add_echo_profile(host)
    space = ok(native, 'spaces.create', {'name': 'bound-event-space'}, idempotency_key='native-event-space-0001')['space_id']
    assistant = ok(native, 'background_agents.create', {'space_id': space, 'name': 'event-worker'},
        idempotency_key='native-event-worker-0001')['agent']['agent_id']
    binding = ok(native, 'interfaces.bind', {'surface_ref': 'gateway', 'name': 'signed-source', 'space_ids': [space],
        'default_space_id': space, 'controller': {'mode': 'agent', 'agent_id': assistant}},
        idempotency_key='native-event-binding-0001')['binding_id']
    from agent_py_agent.tests.test_business_v12_f4_workflows import definition
    ok(native, 'workflows.create', {'definition': definition('native-source-policy'),
        'scope': {'scope_mode': 'spaces', 'space_ids': [space]}}, idempotency_key='native-event-policy-0001')
    sub = ok(native, 'subscriptions.create', {'space_id': space, 'source_ref': binding,
        'policy_ref': 'workflow:native-source-policy'},
        idempotency_key='native-event-subscribe-0001')['subscription']['subscription_id']
    token = ok(native, 'inbox.tokens.mint', {'space_id': space, 'label': 'scoped-service-token', 'subscription_id': sub},
        idempotency_key='native-event-token-0001')
    body = {'target_owner': host.home_paths.owner_id, 'instruction': 'compute 7 plus 9', 'idempotency_key': 'native-event-one',
        'event_id': 'event-one', 'subscription_id': sub, 'source_ref': binding, 'event_revision': 1}
    from agent_py_agent.agent.gateway_parts import remote_push_service
    persist = remote_push_service.persist_push_json
    def lose_accept_receipt(path, value):
        if path.name == 'receipts.json' and any(row.get('accepted') for row in value.get('entries', {}).values()):
            raise OSError('controlled ACK persistence loss')
        return persist(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(remote_push_service, 'persist_push_json', lose_accept_receipt)
        status, uncertain = _signed_native_push(native, body, token['secret'])
    assert status == 503 and uncertain['effect_outcome'] == 'unknown'
    original, = host.conversation_store.session_tasks.list_report(limit=0)[0]
    token = ok(native, 'inbox.tokens.rotate', {'fingerprint': token['token']['fingerprint']},
        idempotency_key='native-event-rotate-0001')
    status, first = _signed_native_push(native, body, token['secret'])
    assert status == 202 and first['task_id'] == original.task_id, first
    task = host.conversation_store.session_tasks.require(first['task_id'])
    assert task.target_thread_id == assistant
    assert task.metadata['business_context']['space_id'] == space
    assert task.metadata['business_context']['binding_id'] == binding
    assert not host.conversation_store.threads.require(task.sender_thread_id).channel_bindings
    status, duplicate = _signed_native_push(native, body, token['secret'])
    assert status == 202 and duplicate['task_id'] == first['task_id'] and duplicate['duplicate']
    # 特定订阅令牌不能省略范围降格成普通ask，也不能自行更换来源/Owner。
    plain_body = {key: value for key, value in body.items() if key not in {'subscription_id', 'source_ref', 'event_revision', 'event_id'}}
    for forged in (plain_body, {**body, 'source_ref': 'other-source'}, {**body, 'target_owner': 'local/other'}):
        status, refused = _signed_native_push(native, forged, token['secret'])
        assert status == 403 and refused['error_code'] == 'REMOTE_PUSH_TOKEN_SCOPE_DENIED'
    legacy = ok(native, 'inbox.tokens.mint', {'space_id': space, 'label': 'ordinary-push'},
        idempotency_key='native-event-legacy-token-0001')
    status, refused = _signed_native_push(native, body, legacy['secret'])
    assert status == 403 and refused['error_code'] == 'REMOTE_PUSH_TOKEN_SCOPE_DENIED'
    status, ordinary = _signed_native_push(native, plain_body, legacy['secret'])
    assert status == 202 and ordinary['run_id'] and 'task_id' not in ordinary
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    with monkeypatch.context() as patch:
        patch.setattr(host, '_current_run_params', RunParams(task_id=task.task_id, source='background_main_agent',
            business_context=BusinessExecutionContext.from_dict(task.metadata['business_context']),
            task_attributes={'conversation_thread_id': task.target_thread_id}), raising=False)
        with pytest.raises(HostAPIError, match='BUSINESS_ACTOR_SOURCE_MISSING'):
            api._model_business_actor(host)
    scheduler = scheduler_for(host)
    tick(scheduler, assistant, __import__('time').time())
    assert host.conversation_store.session_tasks.require(task.task_id).status == 'done'
    ok(native, 'subscriptions.pause', {'subscription_id': sub}, expected_revision=0,
        idempotency_key='native-event-pause-0001')
    status, paused = _signed_native_push(native, {**body, 'event_id': 'paused-event'}, token['secret'])
    assert status == 409 and paused['error_code'] == 'SUBSCRIPTION_NOT_ACTIVE'
    ok(native, 'subscriptions.resume', {'subscription_id': sub}, expected_revision=1,
        idempotency_key='native-event-resume-0001')
    other = ok(native, 'spaces.create', {'name': 'policy-other-space'}, idempotency_key='native-policy-other-0001')['space_id']
    scope = ok(native, 'workflows.scope.get', {'workflow_ref': 'native-source-policy'})
    ok(native, 'workflows.scope.set', {'workflow_ref': 'native-source-policy',
        'scope': {'scope_mode': 'spaces', 'space_ids': [other]}}, expected_revision=scope['scope_revision'],
        idempotency_key='native-policy-move-0001')
    status, policy_denied = _signed_native_push(native, {**body, 'event_id': 'policy-denied-event'}, token['secret'])
    assert status == 403 and policy_denied['error_code'] == 'POLICY_AUTHORIZATION_REQUIRED'
    ok(native, 'interfaces.unbind', {'binding_id': binding}, expected_revision=0,
        idempotency_key='native-event-unbind-0001')
    status, unbound = _signed_native_push(native, {**body, 'event_id': 'unbound-event'}, token['secret'])
    assert status == 409 and unbound['error_code'] == 'SOURCE_ROUTE_UNBOUND'
    assert unbound['effect_outcome'] == 'not_started'
    ok(native, 'inbox.tokens.revoke', {'fingerprint': token['token']['fingerprint']},
        idempotency_key='native-event-revoke-0001')
    status, refused = _signed_native_push(native, body, token['secret'])
    assert status == 401 and refused['error_code'] == 'REMOTE_PUSH_TOKEN_INVALID'
    assert len(host.conversation_store.session_tasks.list_report(limit=0)[0]) == 1


def test_subscription_cursor_failure_keeps_original_task_and_committed_receipt(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.managed_subscriptions import SourceEvent
    from agent_py_agent.tests.test_business_v12_f7_observation import subscription_fixture

    case = subscription_fixture(tmp_path)
    sub = case.subscriptions.execute('subscriptions.create', {'space_id': case.space, 'source_ref': 'comments'},
        actor=case.subscription_actor, idempotency_key='cursor-sub')['subscription']['subscription_id']
    event = SourceEvent('comments', 'cursor-event', 1, 'data')
    transact = case.catalog._transaction
    def interrupted(action, params, **kwargs):
        if action == 'subscriptions.update':
            raise BusinessError('REVISION_CONFLICT')
        return transact(action, params, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(case.catalog, '_transaction', interrupted)
        with pytest.raises(BusinessError) as caught:
            case.subscriptions.accept_event(sub, event, actor=case.subscription_actor, source_identity=case.source_identity)
    assert caught.value.code == 'REVISION_CONFLICT' and caught.value.effect_outcome == 'committed'
    assert caught.value.details['side_effect_state'] == 'task_accepted_cursor_pending'
    task_id = caught.value.details['task_id']
    assert case.store.session_tasks.require(task_id).status == 'queued'
    restored = case.subscriptions.accept_event(sub, event, actor=case.subscription_actor, source_identity=case.source_identity)
    assert restored['task_id'] == task_id
    assert len(case.store.session_tasks.list_report(limit=0)[0]) == 2  # 原夹具任务+一个来源事件。


def test_signed_event_changed_target_requires_owner_resolution_before_dispatch(native, tmp_path):
    """原签名入口返回可处置的未启动状态；配置变化不继承旧token跨空间权限。"""
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
    from agent_py_agent.tests.test_business_v12_background_scheduling import ok
    _, host = native
    wire_business_host_ports(host)
    a, b = [ok(native, 'spaces.create', {'name': 'source-resolution-' + label},
               idempotency_key='source-resolution-space-' + label)['space_id'] for label in ('a', 'b')]
    binding = ok(native, 'interfaces.bind', {'surface_ref': 'gateway', 'name': 'original-source',
        'space_ids': [a], 'default_space_id': a, 'controller': {'mode': 'global'}},
        idempotency_key='source-resolution-binding')['binding_id']
    sub = ok(native, 'subscriptions.create', {'space_id': a, 'source_ref': binding},
        idempotency_key='source-resolution-subscription')['subscription']['subscription_id']
    token = ok(native, 'inbox.tokens.mint', {'space_id': a, 'subscription_id': sub, 'label': 'original-target-only'},
        idempotency_key='source-resolution-token')
    ok(native, 'interfaces.update', {'binding_id': binding, 'space_ids': [b], 'default_space_id': b},
        expected_revision=0, idempotency_key='source-resolution-change')
    body = {'target_owner': host.home_paths.owner_id, 'instruction': '原测试事件，不应执行',
        'idempotency_key': 'source-resolution-event', 'event_id': 'source-resolution-event',
        'subscription_id': sub, 'source_ref': binding, 'event_revision': 1}
    status, refused = _signed_native_push(native, body, token['secret'])
    assert status == 403 and refused['error_code'] == 'SOURCE_SCOPE_MISMATCH', refused
    assert refused['effect_outcome'] == 'not_started'
    details = refused['details']
    assert details['resolution_state'] == 'operator_action_required'
    assert details['retry_same_request'] is False
    assert 'interfaces.resolve' in details['required_actions'] and 'inbox.tokens.mint' in details['required_actions']
    assert not host.conversation_store.session_tasks.list_report(limit=0)[0]
    assert not host.conversation_store.wakes.pending(limit=20)
    import json
    (tmp_path / 'signed-event-resolution-receipt.json').write_text(json.dumps(
        {'http': status, 'response': refused, 'binding_id': binding, 'old_space': a, 'new_space': b,
         'task_count': 0, 'wake_count': 0, 'model_execution': False}, ensure_ascii=False, indent=2))


def test_active_native_subscription_retains_exact_bound_controller_until_paused(native):
    from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
    from agent_py_agent.tests.test_business_v12_background_scheduling import add_echo_profile, ok
    _, host = native
    wire_business_host_ports(host)
    add_echo_profile(host)
    space = ok(native, 'spaces.create', {'name': 'retirement-source'}, idempotency_key='retirement-source-space')['space_id']
    agent = ok(native, 'background_agents.create', {'space_id': space, 'name': 'subscription-target'},
        idempotency_key='retirement-source-agent')['agent']['agent_id']
    binding = ok(native, 'interfaces.bind', {'surface_ref': 'gateway', 'name': 'subscription-source',
        'space_ids': [space], 'default_space_id': space, 'controller': {'mode': 'agent', 'agent_id': agent}},
        idempotency_key='retirement-source-binding')['binding_id']
    sub = ok(native, 'subscriptions.create', {'space_id': space, 'source_ref': binding},
        idempotency_key='retirement-source-subscription')['subscription']['subscription_id']
    thread = host.conversation_store.threads.require(agent)
    assert 'subscription:' + sub in identity_outstanding(host, thread)[0]
    before = ok(native, 'background_agents.get', {'agent_id': agent})
    retired = ok(native, 'background_agents.retire', {'agent_id': agent, 'disposition': 'drain'},
        expected_revision=before['agent']['revision'], idempotency_key='retirement-source-retire')
    assert retired['agent']['identity_state'] == 'retiring'
    assert 'subscription:' + sub in retired['outstanding_refs']
    ok(native, 'subscriptions.pause', {'subscription_id': sub}, expected_revision=0,
       idempotency_key='retirement-source-pause')
    assert 'subscription:' + sub not in identity_outstanding(host, thread)[0]
    assert ok(native, 'background_agents.get', {'agent_id': agent})['agent']['identity_state'] == 'retired'


@pytest.mark.parametrize('fault', ['row_owner', 'binding_owner', 'unbound', 'space', 'global', 'missing'])
def test_subscription_controller_resolution_never_borrows_other_scope(fault):
    from agent_py_agent.agent.conversation.background_team_lifecycle import (
        bound_subscription_controller,
    )
    row = {'owner_id': 'owner-a', 'space_id': 'space-a', 'source_ref': 'binding-a'}
    binding = {'owner_id': 'owner-a', 'state': 'bound', 'space_ids': ['space-a'],
               'default_space_id': 'space-a', 'controller': {'mode': 'agent', 'agent_id': 'bg-a'}}
    state = {'bindings': {'binding-a': binding}}
    assert bound_subscription_controller(state, row, 'owner-a') == 'bg-a'
    if fault == 'row_owner':
        row['owner_id'] = 'owner-b'
    elif fault == 'binding_owner':
        binding['owner_id'] = 'owner-b'
    elif fault == 'unbound':
        binding['state'] = 'unbound'
    elif fault == 'space':
        row['space_id'] = 'space-b'
    elif fault == 'global':
        binding['controller'] = {'mode': 'global'}
    else:
        state['bindings'].clear()
    assert bound_subscription_controller(state, row, 'owner-a') is None
