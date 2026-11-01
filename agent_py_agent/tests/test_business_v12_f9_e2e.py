"""W15 组合验收：走真实 HTTP、原存储和鉴权，不替产品补线或补产物。"""
import json
import subprocess

import pytest

from agent_py_agent.tests.test_mega_business_api_actions import native_request


@pytest.fixture
def native_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'host-home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


def call(host, action, params=None, **extra):
    status, _, raw = native_request(host, 'POST', '/client/business', {
        'action': action, 'request_id': 'w15-' + action,
        'params': params or {}, **extra,
    })
    return status, json.loads(raw)


def evidence(record_property, case_id):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    record_property('candidate_sha', head)
    record_property('case_id', case_id)
    record_property('validation_level', 'service')
    record_property('observation_point', 'authenticated_native_loopback_http')


def test_empty_purpose_can_be_updated_without_recreating_space(native_http, record_property):
    evidence(record_property, 'V12-002')
    status, first = call(native_http, 'spaces.create', {'name': '组合空间'},
                         idempotency_key='w15-empty-purpose-create')
    assert status == 200, first
    space = first['data']
    assert space['purpose'] == ''
    status, updated = call(native_http, 'spaces.update', {
        'space_id': space['space_id'], 'purpose': '后来明确的用途'},
        expected_revision=space['revision'], idempotency_key='w15-empty-purpose-update')
    assert status == 200, updated
    assert updated['data']['space']['space_id'] == space['space_id']
    assert updated['data']['space']['purpose'] == '后来明确的用途'
    assert updated['data']['space']['revision'] > space['revision']


def test_same_name_conflict_and_original_operation_replay(native_http, record_property):
    evidence(record_property, 'V12-003')
    status, first = call(native_http, 'spaces.create', {'name': '同名空间'},
                         idempotency_key='w15-name-original-operation')
    assert status == 200, first
    assert call(native_http, 'spaces.create', {'name': '同名空间'},
                idempotency_key='w15-name-original-operation')[1]['data'] == first['data']
    status, conflict = call(native_http, 'spaces.create', {'name': '同名空间'},
                            idempotency_key='w15-name-other-operation')
    assert status >= 400, conflict
    status, listed = call(native_http, 'spaces.list')
    assert status == 200, listed
    assert [row['space_id'] for row in listed['data']['items']] == [first['data']['space_id']]


def test_native_space_to_background_identity_is_really_connected(native_http, record_property):
    evidence(record_property, 'V12-022')
    status, created = call(native_http, 'spaces.create', {'name': '后台所属空间'},
                           idempotency_key='w15-background-space-create')
    assert status == 200, created
    status, result = call(native_http, 'background_agents.create', {
        'space_id': created['data']['space_id'], 'name': '后台执行者'},
        idempotency_key='w15-background-identity-create')
    assert status == 200, result
    agent = result['data']['agent']
    assert agent['agent_id'] == agent['thread_id']
    assert agent['space_id'] == created['data']['space_id']
    status, fetched = call(native_http, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert status == 200, fetched
    assert fetched['data']['agent']['thread_id'] == agent['thread_id']
    assert fetched['data']['current_execution'] is None


def test_native_read_does_not_claim_missing_services_are_supported(native_http):
    status, first = call(native_http, 'system.capabilities')
    assert status == 200 and first['effect_outcome'] == 'none'
    assert 'spaces.create' in first['data']['supported_actions']
    for action in first['data']['supported_actions']:
        assert isinstance(action, str) and action
    assert call(native_http, 'system.capabilities')[1]['data'] == first['data']


def test_all_102_actions_reject_forged_scope_before_domain_execution(native_http, record_property):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs

    actions = sorted(business_action_specs())
    assert len(actions) == 102
    for action in actions:
        status, result = call(native_http, action, {'forged_owner': 'business-bob'})
        assert status == 400, (action, result)
        assert result['error']['code'] == 'INVALID_SCHEMA', (action, result)
        assert result['effect_outcome'] == 'not_started', (action, result)
    record_property('action_matrix_dimension', 'invalid_schema_no_execution')
    record_property('tested_actions', json.dumps(actions))


def test_all_actions_and_both_media_routes_require_real_gateway_auth(native_http, record_property):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs

    for action in sorted(business_action_specs()):
        status, _, raw = native_request(native_http, 'POST', '/client/business', {
            'action': action, 'request_id': 'w15-denied', 'params': {}},
            headers={'X-Gateway-Token': ''})
        assert status == 403, (action, raw)
    routes = [('POST', '/client/business/artifacts'),
              ('GET', '/client/business/artifacts/unknown-artifact/content')]
    for method, path in routes:
        status, _, raw = native_request(native_http, method, path, headers={'X-Gateway-Token': ''})
        assert status == 403, (method, path, raw)
    record_property('action_matrix_dimension', 'authentication_denied')
    record_property('tested_actions', json.dumps(sorted(business_action_specs())))
    record_property('tested_media_routes', json.dumps(routes))


def test_owner_b_cannot_read_owner_a_space_through_real_native_transport(native_http):
    status, created = call(native_http, 'spaces.create', {'name': 'Alice私人空间'},
        idempotency_key='w15-owner-a-private-space')
    assert status == 200, created
    body = {'action': 'spaces.get', 'request_id': 'w15-owner-b-read',
            'params': {'space_id': created['data']['space_id']}}
    status, _, raw = native_request(native_http, 'POST', '/client/business', body, user='business-bob')
    assert status == 404, raw
    assert 'Alice私人空间' not in raw.decode()
    assert call(native_http, 'spaces.get', body['params'])[0] == 200
