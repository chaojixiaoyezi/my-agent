"""#183 业务会话列表：真实认证 HTTP、W1 等价性和冻结注册表回归。"""
from __future__ import annotations

import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.gateway_parts import business_api_v13_w11 as w11
from agent_py_agent.agent.gateway_parts.owner_conversation_store import owner_conversation_store
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests import test_mega_business_api_actions as harness

background_http = harness.background_http
background_call = harness.background_call
native_request = harness.native_request


# LLM: 只用原 owner 路径裁决和 Store writer 准备测试数据，不替换任何读取或认证逻辑。
# 函数用途: 给指定身份写入一条真实会话绑定和消息。
def seed_session(case, user, conversation_id, *, channel='feishu'):
    _, base = case
    owner = (OwnerIdentity.local_main() if channel == 'chat'
        else OwnerIdentity.provider_user(channel, user))
    scoped = home_paths_with_owner(base.home_paths, resolve_owner_home(base.home_paths.root, owner))
    store = owner_conversation_store(base, scoped, initialize=True)
    thread = store.threads.get_or_create({'canonical_user_id': user, 'channel': channel,
        'channel_conversation_id': conversation_id, 'channel_user_id': user,
        'owner_id': scoped.owner_id, 'owner_home': str(scoped.owner_home_dir)})
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user',
        'content': '会话内容 ' + conversation_id, 'metadata': {'gateway_request_id': conversation_id}})
    return store, thread, scoped.owner_id


# LLM: 管理员请求只带真实 Gateway 凭据；不伪造身份头，也不从 JSON 选择 owner。
# 函数用途: 使用无身份头的原认证入口发送请求。
def admin_request(case, path, data):
    server, _ = case
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
    try:
        connection.request('POST', path, json.dumps(data), headers={
            'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 同一身份、limit、cursor 和在途选项经过两个真实 HTTP 路由，逐页比较完整业务数据。
# 函数用途: 核对业务入口与 W1 列表相同，并通过 v13 专用发现入口展示能力。
@pytest.mark.parametrize('include_inflight', [None, True, False])
def test_sessions_list_matches_http_list(background_http, include_inflight):
    expected = set()
    for index in range(3):
        _, thread, owner_id = seed_session(background_http, 'business-alice', f'alice-page-{index}')
        expected.add(thread.thread_id)
    params = {'limit': 1}
    if include_inflight is not None:
        params['include_inflight'] = include_inflight
    seen = set()
    for _ in range(3):
        status, _, raw = native_request(background_http, 'POST', '/client/history',
            {'operation': 'list', **params})
        history = json.loads(raw)
        assert status == 200 and history['ok'] is True, history
        status, result = background_call(background_http, 'sessions.list', params)
        assert status == 200 and result['error'] is None, result
        data = result['data']
        assert data == {key: history[key] for key in (
            'owner_id', 'items', 'next_cursor', 'coverage', 'inflight_coverage', 'load_errors')}
        assert data['owner_id'] == owner_id
        assert len(data['items']) == 1
        thread_id = data['items'][0]['thread_id']
        assert thread_id not in seen
        seen.add(thread_id)
        if data['next_cursor'] is not None:
            params['cursor'] = data['next_cursor']
    assert seen == expected and data['next_cursor'] is None
    status, capabilities = background_call(background_http, 'system.capabilities.extensions')
    assert status == 200 and 'sessions.list' in capabilities['data']['supported_actions'], capabilities
    # 冻结 system.capabilities 只列原生冻结动作；扩展不能污染它。
    status, frozen = background_call(background_http, 'system.capabilities')
    assert status == 200 and 'sessions.list' not in frozen['data']['supported_actions']


# LLM: A/B 身份及无身份头的管理员交错请求；同 owner 的其它绑定也必须被过滤。
# 函数用途: 防止 owner、viewer 或缓存请求闭包串用。
def test_sessions_list_owner_isolation(background_http):
    alice_store, alice, alice_owner = seed_session(background_http, 'business-alice', 'alice-only')
    _, bob, bob_owner = seed_session(background_http, 'business-bob', 'bob-only')
    _, tui, tui_owner = seed_session(background_http, 'local-agent', 'sess_local_tui', channel='chat')
    hidden = alice_store.threads.get_or_create({'canonical_user_id': 'business-bob', 'channel': 'feishu',
        'channel_conversation_id': 'not-alice-binding', 'channel_user_id': 'business-bob',
        'owner_id': alice_owner})
    for user, thread, owner_id in (('business-alice', alice, alice_owner),
            ('business-bob', bob, bob_owner), ('business-alice', alice, alice_owner)):
        status, result = background_call(background_http, 'sessions.list', user=user)
        assert status == 200 and result['data']['owner_id'] == owner_id, result
        assert {item['thread_id'] for item in result['data']['items']} == {thread.thread_id}
        assert hidden.thread_id not in str(result)
    status, result = admin_request(background_http, '/client/business',
        {'action': 'sessions.list', 'request_id': 'list-main-owner', 'params': {}})
    assert status == 200 and result['data']['owner_id'] == tui_owner, result
    assert {item['thread_id'] for item in result['data']['items']} == {tui.thread_id}
    assert alice.thread_id not in str(result) and bob.thread_id not in str(result)


# LLM: 两次真实认证HTTP共享owner与services缓存，仅在首个上下文构建后暂停；不替换认证、列表读取或响应。
#   第二请求完成后再释放首请求，两页的绑定与预览必须各自保持当前viewer；异常也释放Event避免挂住收尾。
# 函数用途: 固定复现共享owner下请求闭包串用，验证交错请求不会读到对方会话。
@pytest.mark.parametrize('first_user,second_user', [
    ('business-alice', 'business-bob'), ('business-bob', 'business-alice'),
])
def test_sessions_list_keeps_request_viewer_during_interleaving(background_http, monkeypatch,
        first_user, second_user):
    _, base = background_http
    base.config.gateway_per_user_owner_scoping = False
    store = owner_conversation_store(base, base.home_paths, initialize=True)
    expected = {}
    for user in (first_user, second_user):
        conversation_id = f'{user}-shared-owner'
        thread = store.threads.get_or_create({'canonical_user_id': user, 'channel': 'feishu',
            'channel_conversation_id': conversation_id, 'channel_user_id': user,
            'owner_id': base.home_paths.owner_id, 'owner_home': str(base.home_paths.owner_home_dir)})
        preview = f'会话预览 {user}'
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': preview,
            'metadata': {'gateway_request_id': conversation_id}})
        expected[user] = (thread.thread_id, user, conversation_id, preview)

    entered, release = Event(), Event()
    contexts = {}
    original = api.native_business_context

    # LLM: 仅调度两个真实请求的先后，原上下文、认证与owner裁决照常执行。
    # 函数用途: 等首请求拿到上下文后让第二请求完整走完。
    def interleave(handler, server):
        context, services = original(handler, server)
        contexts[context.subject_id] = (context, services)
        if context.subject_id == first_user:
            entered.set()
            assert release.wait(timeout=5), 'second request did not release the first request'
        return context, services

    monkeypatch.setattr(api, 'native_business_context', interleave)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(background_call, background_http, 'sessions.list', user=first_user)
        try:
            assert entered.wait(timeout=5), 'first request did not construct its context'
            second_status, second_result = background_call(background_http, 'sessions.list', user=second_user)
            assert second_status == 200 and second_result['error'] is None, second_result
            assert second_result['data']['owner_id'] == base.home_paths.owner_id
            assert [(item['thread_id'], item['channel_user_id'], item['conversation_id'], item['preview'])
                for item in second_result['data']['items']] == [expected[second_user]]
            first_context, first_services = contexts[first_user]
            second_context, second_services = contexts[second_user]
            assert first_services is second_services
            assert first_context.host is not second_context.host
            assert first_services.host is second_context.host
        finally:
            release.set()
        first_status, first_result = first.result(timeout=10)
    assert first_status == 200 and first_result['error'] is None, first_result
    assert first_result['data']['owner_id'] == base.home_paths.owner_id
    assert [(item['thread_id'], item['channel_user_id'], item['conversation_id'], item['preview'])
        for item in first_result['data']['items']] == [expected[first_user]]


# LLM: 关闭既有开关仍拒绝业务动作；W1 历史列表不随业务开关失效。
# 函数用途: 保住默认关闭的业务门与始终可用的 HTTP 列表。
def test_sessions_list_disabled_spaces_is_503(background_http):
    seed_session(background_http, 'business-alice', 'alice-visible')
    assert background_call(background_http, 'sessions.list')[0] == 200
    background_http[1].config.business_spaces_enabled = False
    status, result = background_call(background_http, 'sessions.list')
    assert status == 503 and result['error']['code'] == 'HOST_NOT_READY', result
    status, _, raw = native_request(background_http, 'POST', '/client/history', {'operation': 'list'})
    assert status == 200 and json.loads(raw)['ok'] is True
    background_http[1].config.business_spaces_enabled = True
    assert background_call(background_http, 'sessions.list')[0] == 200


# LLM: 无效参数从原 schema 门拒绝，合法长度的坏/外域 cursor 才进入 W1 并归一旧错误码。
# 函数用途: 覆盖边界、额外字段、默认值及当前 CURSOR_EXPIRED 状态。
def test_sessions_list_schema_and_cursor_errors(background_http):
    seed_session(background_http, 'business-alice', 'alice-one')
    seed_session(background_http, 'business-alice', 'alice-two')
    invalid = [{'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': '1'},
        {'unknown': True}, {'owner_id': 'local/main'}, {'cursor': ''}, {'cursor': 'x' * 2049},
        {'cursor': None}, {'include_inflight': 'yes'}]
    for params in invalid:
        status, result = background_call(background_http, 'sessions.list', params)
        assert status == 400 and result['error']['code'] == 'INVALID_SCHEMA', (params, result)
    status, first = background_call(background_http, 'sessions.list', {'limit': 1})
    assert status == 200 and first['data']['next_cursor'], first
    cursor = first['data']['next_cursor']
    for params, user in (({'cursor': '!'}, 'business-alice'), ({'cursor': cursor}, 'business-bob')):
        status, result = background_call(background_http, 'sessions.list', params, user=user)
        assert status == 410 and result['error']['code'] == 'CURSOR_EXPIRED', result
        assert result['data'] == {}
    status, result = background_call(background_http, 'sessions.list', {'limit': 100})
    assert status == 200 and len(result['data']['items']) == 2, result


# LLM: 用当前其它显式分片重载作为 base，比较动作全集、别名及 W11 handler 身份。
# 函数用途: 确认 sessions 分片仍只增加一个无别名动作，没有重定义 conversations.list。
def test_conversations_list_contract_unchanged():
    base_modules = tuple('agent_py_agent.agent.gateway_parts.business_api_v13_w' + str(wave)
        for wave in range(7, 13))
    gap_modules = tuple('agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w' + str(wave)
        for wave in range(1, 10))
    base_actions, base_aliases, base_defs, base_jobs, base_collections = v13._load_registry(
        (*base_modules, *gap_modules))
    actions, aliases, definitions, jobs, collections = v13._load_registry()
    assert set(actions) == set(base_actions) | {'sessions.list'}
    assert aliases == base_aliases
    assert definitions == base_defs and jobs == base_jobs and collections == base_collections
    assert actions['conversations.list'] is base_actions['conversations.list']
    w11_action = next(action for action in w11.V13_ACTIONS if action.name == 'conversations.list')
    assert actions['conversations.list'] is w11_action
    assert w11_action.handler.args == (
        'agent_py_agent.agent.business_spaces.v13_w11_conversations', 'list_conversations')
    assert actions['sessions.list'].aliases == ()
    assert actions['sessions.list'].effect == 'read'
    assert actions['sessions.list'].admin is False
    assert actions['sessions.list'].idempotency_required is False
    expected_modules = (*base_modules, 'agent_py_agent.agent.gateway_parts.business_api_v13_sessions',
                        *gap_modules)
    assert expected_modules == v13.V13_SLICE_MODULES


# LLM: 无原生 host 闭包的插件/CLI 宿主不能宣称会话列表已就绪。
# 函数用途: 核对 readiness 与未知宿主不授予读取能力。
def test_sessions_list_requires_native_host_binding(tmp_path):
    paths = home_paths(tmp_path / 'plugin-home')
    scoped = home_paths_with_owner(paths, resolve_owner_home(paths.root, OwnerIdentity.local_main()))
    host = SimpleNamespace(home_paths=scoped,
        config=SimpleNamespace(business_spaces_enabled=True))
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    status, result = api.execute_business({'action': 'sessions.list', 'request_id': 'no-native-host',
        'params': {}}, context, services)
    assert status == 503 and result['error']['code'] == 'HOST_NOT_READY', result
    status, result = api.execute_business({'action': 'system.capabilities.extensions',
        'request_id': 'missing-host-discovery', 'params': {}}, context, services)
    assert status == 200 and 'sessions.list' not in result['data']['supported_actions'], result


# LLM: 使用无请求context的原services.ready/v13_ready入口，只检查能力绑定，绝不能执行数据闭包。
# 函数用途: 防止修数据读取时把context-free能力查询也改成依赖当前请求。
def test_sessions_list_readiness_without_request_context(tmp_path):
    paths = home_paths(tmp_path / 'readiness-home')
    scoped = home_paths_with_owner(paths, resolve_owner_home(paths.root, OwnerIdentity.local_main()))

    # LLM: readiness只允许检查可调用性，若读数据即为回归。
    # 函数用途: 捕获能力检查不该触发的会话数据访问。
    def forbidden(**params):
        pytest.fail('capability readiness must not read session data')

    host = SimpleNamespace(home_paths=scoped, config=SimpleNamespace(business_spaces_enabled=True),
        business_session_list=forbidden)
    services = api.BusinessServices(host)
    assert services.ready('sessions.list') is True
    assert v13.v13_ready('sessions.list', services, context=None) is True
    host.config.business_spaces_enabled = False
    assert services.ready('sessions.list') is False
    assert v13.v13_ready('sessions.list', services, context=None) is False
