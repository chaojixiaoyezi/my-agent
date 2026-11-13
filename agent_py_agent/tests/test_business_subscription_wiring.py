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
