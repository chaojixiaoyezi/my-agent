"""业务原原因、写前拒绝与未知写入在原工具控制边界保持独立。"""
import json

import pytest

from agent_py_agent.agent.gateway_parts.business_api import _business_tool_control_code
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests import test_business_model_tools as original
from agent_py_agent.tests import test_mega_business_api_actions as native

agent = original.agent


@pytest.mark.parametrize('code', ['SPACE_MISMATCH', 'AGENT_NOT_AVAILABLE', 'BUSINESS_ACCESS_DENIED',
                                 'ACTOR_REQUIRED', 'SPACE_SCOPE_MISMATCH'])
def test_known_business_rejection_keeps_precise_nonretryable_control(code):
    result = ToolHandlerOutcome('business', False, '{}', error_code=_business_tool_control_code(code, 'not_started'),
        reported_error_code=code, effect_outcome='not_started')
    assert result.error_code == result.reported_error_code == code
    assert result.error_category in {'state', 'permission'}
    assert not result.retryable and result.recommended_action != 'report_blocker'


@pytest.mark.parametrize('code', ['SPACE_MISMATCH', 'REVISION_CONFLICT', 'STORE_UNAVAILABLE', 'NEW_PROVIDER_CODE'])
def test_uncertain_write_cannot_inherit_not_started_recovery(code):
    result = ToolHandlerOutcome('business', False, '{}', error_code=_business_tool_control_code(code, 'unknown'),
        reported_error_code=code, effect_outcome='unknown')
    assert result.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert result.reported_error_code == code and result.effect_outcome == 'unknown'
    assert not result.retryable and result.recommended_action == 'manual_review'


def test_unregistered_preflight_code_stays_unknown():
    result = ToolHandlerOutcome('business', False, '{}',
        error_code=_business_tool_control_code('NOT_A_REGISTERED_CODE', 'not_started'), effect_outcome='not_started')
    assert result.error_code == 'UNKNOWN_ERROR'


def test_native_tool_precise_permission_rejection_and_old_tool_live_disable(agent):
    tool = agent.tools.tools['business_execution']
    thread_id = agent._current_run_params.task_attributes['conversation_thread_id']
    result = tool.execute({'action': 'background_agents.messages.send', 'request_id': 'permission-before',
        'params': {'agent_id': thread_id, 'text': 'synthetic', 'input_kind': 'message'}, 'idempotency_key': 'permission-rejection-0001'})
    assert not result.ok
    assert result.error_code == result.reported_error_code == 'BUSINESS_ACCESS_DENIED'
    assert result.effect_outcome == 'not_started'
    assert json.loads(result.output)['effect_outcome'] == 'not_started'
    cached = agent.tools.tools['business_space']
    assert cached.execute({'action': 'spaces.list', 'request_id': 'before-revoke', 'params': {}}).ok
    agent.config.business_spaces_enabled = False
    revoked = cached.execute({'action': 'spaces.list', 'request_id': 'after-revoke', 'params': {}})
    assert not revoked.ok and revoked.error_code == 'HOST_NOT_READY'
    assert revoked.effect_outcome == 'not_started'


native_http = native.native_http


def test_two_original_clients_credential_revocation_has_frozen_business_envelope(native_http):
    import http.client

    from agent_py_agent.cli.space_commands import BusinessClient

    server, base = native_http
    credential = server._local_client_credential
    connections = [http.client.HTTPConnection('127.0.0.1', server.server.server_address[1]) for _ in range(2)]
    raw = []

    def transport(connection):
        def send(path, payload, timeout=10):
            connection.request('POST', path, json.dumps(payload), {
                'Content-Type': 'application/json', 'X-Gateway-Token': credential,
                'X-User-Id': 'synthetic-client', 'X-Channel': 'feishu'})
            response = connection.getresponse()
            body = json.loads(response.read())
            raw.append((response.status, body))
            return response.status, body
        return send

    try:
        clients = [BusinessClient(transport(connection)) for connection in connections]
        space = clients[0].call('spaces.create', {'name': 'credential-live-revoke'})['data']['space_id']
        before = [client.call('spaces.get', {'space_id': space}) for client in clients]
        assert all(item['ok'] for item in before)
        assert before[0]['data'] == before[1]['data']
        server.auth_middleware.set_local_client_credential('')
        for client in clients:
            for _ in range(2):
                rejected = client.call('spaces.get', {'space_id': space})
                assert not rejected['ok'] and rejected['http_status'] == 403, rejected
                assert rejected['error']['code'] == 'LOCAL_CREDENTIAL_REQUIRED'
                assert rejected['effect_outcome'] == 'not_started'
                assert not rejected['error']['retryable']
                assert any(item['key'] == 'next_action' for item in rejected['error']['details'])
        status, streamed = transport(connections[0])('/client/business/stream', {
            'action': 'events.list', 'request_id': 'denied-stream', 'params': {}})
        assert status == 403 and streamed['effect_outcome'] == 'not_started'
        assert streamed['error']['code'] == 'LOCAL_CREDENTIAL_REQUIRED'
        status, legacy = transport(connections[0])('/ask', {'prompt': 'must not execute'})
        assert status == 403 and legacy['error'] == 'forbidden'
        assert legacy['error_code'] == 'LOCAL_CREDENTIAL_REQUIRED'
    finally:
        for connection in connections:
            connection.close()


def test_foreign_space_domain_reason_survives_handler_executor_native_renderer(agent, tmp_path):
    from agent_py_agent.agent.backends.message_adapter import _tool_result_block
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    from agent_py_agent.tests.test_business_v12_f2_spaces import World

    bob = World(tmp_path / 'bob', 'owner-bob')
    foreign = bob.create('Bob private')['space_id']
    request = {'action': 'spaces.get', 'request_id': 'foreign-space-read', 'params': {'space_id': foreign}}
    handler = agent.tools.tools['business_space'].execute(request)
    assert handler.error_code == handler.reported_error_code == 'SPACE_NOT_FOUND'
    assert handler.host_error['source'] == 'business_host' and handler.host_error['http_status'] == 404
    result = execute_registry_test_call(agent.tools, 'business_spaces_get', request, call_id='foreign-native-call')
    body = json.loads(result.output)
    assert not result.ok and result.error_code == result.metadata['reported_error_code'] == 'SPACE_NOT_FOUND'
    assert body['error']['code'] == 'SPACE_NOT_FOUND' and body['data'] == {}
    assert result.effect_outcome == 'not_started' and not result.retryable
    block = _tool_result_block(result)
    assert block['tool_use_id'] == 'foreign-native-call' and block['is_error'] is True
    assert 'error_code=SPACE_NOT_FOUND' in block['content'] and 'UNKNOWN_ERROR' not in block['content']
    assert 'Bob private' not in block['content']
    assert bob.call('spaces.list')['items'][0]['space_id'] == foreign
    # 相同JSON若没有宿主显式错误事实，不能从正文取得分类权。
    untrusted = ToolHandlerOutcome('provider', False, result.output, error_code='UNKNOWN_ERROR')
    assert untrusted.error_code == untrusted.reported_error_code == 'UNKNOWN_ERROR'


def test_duplicate_space_create_keeps_precise_domain_code_and_original_object(agent):
    from agent_py_agent.agent.backends.message_adapter import _tool_result_block
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    created = original.business_call(agent, 'spaces.create', {'name': 'exact original'}, idempotency_key='first-name')
    request = {'action': 'spaces.create', 'request_id': 'duplicate-name',
        'params': {'name': 'exact original'}, 'idempotency_key': 'distinct-intent-second-0001'}
    result = execute_registry_test_call(agent.tools, 'business_spaces_create', request, call_id='duplicate-native-call')
    assert not result.ok and result.error_code == result.metadata['reported_error_code'] == 'SPACE_NAME_CONFLICT'
    assert json.loads(result.output)['error']['code'] == 'SPACE_NAME_CONFLICT'
    assert result.effect_outcome == 'not_started' and not result.retryable
    assert 'error_code=SPACE_NAME_CONFLICT' in _tool_result_block(result)['content']
    items = original.business_call(agent, 'spaces.list')['items']
    assert len(items) == 1 and items[0]['space_id'] == created['space_id']
