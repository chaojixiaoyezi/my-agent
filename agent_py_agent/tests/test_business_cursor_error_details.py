"""失效游标通过真实原生 HTTP 返回可恢复诊断，不误报临时服务失败。"""
import base64
import hmac
import json

import pytest

from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    native_request,
    observed_native_task,
    open_native_observation,
    seed_observed_runtime,
)


@pytest.mark.parametrize('reason', ['expired', 'scope_changed', 'invalid_cursor'])
def test_cursor_rejection_client_and_stream_recover(background_http, reason):
    services, task = observed_native_task(background_http)
    seed_observed_runtime(services, task)

    def transport(path, request, **kwargs):
        status, _, raw = native_request(background_http, 'POST', path, request)
        return status, json.loads(raw)

    client = BusinessClient(transport)
    first = client.call('events.list', {'limit': 1, 'filter': {}})
    assert first['ok'] and first['data']['events']
    cursor = first['data']['next_cursor']
    filters = {}
    if reason == 'expired':
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
        state = json.loads(raw[:-32])
        state['expires'] = 1
        raw = json.dumps(state, separators=(',', ':')).encode()
        cursor = base64.urlsafe_b64encode(raw + hmac.digest(services.observation._cursor_key, raw, 'sha256')).decode().rstrip('=')
    elif reason == 'scope_changed':
        filters = {'space_id': task.metadata['business_context']['space_id']}
    else:
        cursor = '!'
    reply = client.call('events.list', {'cursor': cursor, 'filter': filters})
    assert reply['http_status'] == 410 and not reply['ok']
    assert reply['effect_outcome'] == 'not_started' and reply['data'] == {}
    error = reply['error']
    assert error['code'] == 'CURSOR_EXPIRED' and not error['retryable']
    details = {item['key']: item['value'] for item in error['details']}
    assert details == {'reason': reason, 'reset_required': True,
        'next_action': 'discard_cursor_and_request_fresh_snapshot', 'retry_same_cursor': False}
    connection, response = open_native_observation(background_http, {
        'request_id': 'rejected-cursor', 'cursor': cursor, 'filter': filters})
    try:
        assert response.status == 200
        rejected = json.loads(response.readline())
        assert rejected['stream_item'] == 'cursor_expired'
        assert rejected['payload']['reset_required'] is True
        assert rejected['payload']['reason_code'] == 'CURSOR_EXPIRED'
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()
    recovered = client.call('events.list', {'filter': filters})
    assert recovered['ok'] and recovered['data']['events']
    connection, response = open_native_observation(background_http, {
        'request_id': 'fresh-snapshot', 'filter': filters})
    try:
        assert response.status == 200
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == 'queued'
