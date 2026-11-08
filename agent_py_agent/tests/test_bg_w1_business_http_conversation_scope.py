"""业务入口缺少请求头时安全降级；真实HTTP继续保留会话及认证边界。"""
from __future__ import annotations

import http.client
import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)


@pytest.fixture
def host(tmp_path):
    home = tmp_path / 'home'
    return SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False, my_agent_home=str(home),
        memory_path='memory.jsonl', business_spaces_enabled=True), tmp_path / 'workspace')


def _thread(host, conversation, *, user='admin', channel='chat'):
    return host.conversation_store.threads.get_or_create({
        'canonical_user_id': user, 'channel': channel, 'channel_conversation_id': conversation,
        'channel_user_id': user, 'owner_id': host.home_paths.owner_id,
        'owner_home': str(host.home_paths.owner_home_dir)})


@pytest.mark.parametrize('kind', ['missing', 'none', 'empty', 'selected'])
def test_native_context_defaults_missing_headers_and_preserves_selected_scope(host, kind):
    handler = SimpleNamespace()
    if kind == 'none':
        handler.headers = None
    elif kind in ('empty', 'selected'):
        handler.headers = Message()
        if kind == 'selected':
            handler.headers['X-Conversation-Id'] = 'selected'
    source = _thread(host, 'selected' if kind == 'selected' else 'default')
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(host.home_paths.root / 'gateway'))
    context, _ = api.native_business_context(handler, server)
    assert context.actor_factory('background_agents.list').source_thread_id == source.thread_id
    assert context.check('spaces.list', {}) is True


@pytest.mark.parametrize('remove', [False, True])
def test_live_authorization_and_admin_reject_headers_removed_after_binding(host, remove):
    middleware = AuthMiddleware(AuthManager())
    middleware.set_local_client_credential('ci-unit-credential')
    headers = Message()
    for key, value in {'X-Gateway-Token': 'ci-unit-credential', 'X-User-Id': 'admin', 'X-Channel': 'chat'}.items():
        headers[key] = value
    handler = SimpleNamespace(headers=headers, _auth_middleware=middleware, client_address=('127.0.0.1', 1))
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(host.home_paths.root / 'gateway'))
    context, _ = api.native_business_context(handler, server)
    assert context.check('spaces.list', {}) is True
    assert context.host.business_native_admin() is True
    if remove:
        del handler.headers
    else:
        handler.headers = None
    with pytest.raises(HostAPIError) as error:
        context.check('spaces.list', {})
    assert error.value.code == 'CAPABILITY_DENIED'
    assert context.host.business_native_admin() is False


@pytest.mark.parametrize('explicit_none', [False, True])
def test_authenticated_context_without_headers_is_denied(host, explicit_none):
    handler = SimpleNamespace(_auth_middleware=AuthMiddleware(AuthManager()))
    if explicit_none:
        handler.headers = None
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(host.home_paths.root / 'gateway'))
    with pytest.raises(HostAPIError) as error:
        api.native_business_context(handler, server)
    assert error.value.code == 'CAPABILITY_DENIED' and error.value.status == 403


@pytest.mark.parametrize('operation,code', [(api._native_source, 'CAPABILITY_DENIED'), (api._native_json, 'INVALID_SCHEMA')])
@pytest.mark.parametrize('explicit_none', [False, True])
def test_http_helpers_reject_missing_headers_with_protocol_errors(operation, code, explicit_none):
    handler = SimpleNamespace(rfile=io.BytesIO(b'{}'))
    if explicit_none:
        handler.headers = None
    with pytest.raises(HostAPIError) as error:
        operation(handler)
    assert error.value.code == code


@pytest.mark.parametrize('duplicate', [False, True])
def test_json_reader_preserves_raw_duplicate_header_validation(duplicate):
    headers = Message()
    headers['Content-Length'] = '2'
    headers['Content-Type'] = 'application/json'
    if duplicate:
        headers['Content-Length'] = '2'
        with pytest.raises(HostAPIError) as error:
            api._native_json(SimpleNamespace(headers=headers, rfile=io.BytesIO(b'{}')))
        assert error.value.code == 'INVALID_SCHEMA'
    else:
        assert api._native_json(SimpleNamespace(headers=headers, rfile=io.BytesIO(b'{}'))) == {}


def test_real_http_business_scope_preserves_owner_thread_and_missing_scope_rejection(host):
    server = GatewayHTTPServer(0, gateway_paths_from_root(host.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=host))
    server.start()
    identity = _gateway_request_identity(owner_identity_from_config(host.config))
    _thread(host, 'selected', user=identity['user_id'], channel=identity['channel'])

    def post(action, params, conversation=''):
        headers = {'X-Gateway-Token': server._local_client_credential, 'Content-Type': 'application/json',
            'X-User-Id': identity['user_id'], 'X-Channel': identity['channel']}
        if conversation:
            headers['X-Conversation-Id'] = conversation
        body = {'action': action, 'request_id': 'ci-s2', 'params': params}
        connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
        try:
            connection.request('POST', '/client/business', json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, selected = post('background_agents.list', {'limit': 10}, 'selected')
        assert status == 200 and selected['error'] is None and selected['data']['items'] == [], selected
        threads = host.conversation_store.storage.threads_dir
        before = sorted(path.name for path in threads.glob('*.json'))
        for conversation in ('', 'unknown'):
            status, denied = post('background_agents.list', {'limit': 10}, conversation)
            assert status == 403 and denied['error']['code'] == 'ACTOR_REQUIRED', denied
        assert sorted(path.name for path in threads.glob('*.json')) == before
    finally:
        server.stop()
