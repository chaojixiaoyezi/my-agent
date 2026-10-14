"""整机控制面与 owner 业务数据分离、真实 HTTP 入站归属、原群推送；正文仅为合成数据。"""
import json
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.manager import ChannelManager, configure_gateway_client
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.capability.channel_message_target import resolve_message_target
from agent_py_agent.agent.conversation.background_routing import (
    BackgroundRouteDependencies,
    resolve_background_route,
)
from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    bind_request_owner,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    _process_gateway_request_path,
    _resolve_request_agent,
    _resolve_request_owner_identity,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import (
    bind_admin_channel_identity,
    remove_admin_channel_identity,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_gateway_conversation_control import _free_port


def _base(tmp_path, *, full=False):
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False,
        gateway_per_user_owner_scoping=True, admin_channel_identity_enabled=True,
        access_mode='full-access' if full else 'workspace-write')
    agent = SimpleAgent(config, resolve_owner_home(tmp_path/'home', OwnerIdentity.local_main()).home_dir)
    ensure_local_client_credential(agent.home_paths.root)
    return agent


def _payload(actor='ou_one', chat='oc_private', kind='p2p'):
    return {'metadata': {'channel': 'feishu', 'user_id': actor, 'channel_chat_type': kind, 'channel_chat_id': chat},
        'conversation': {'channel': 'feishu', 'channel_user_id': actor, 'channel_conversation_id': chat, 'canonical_user_id': actor}}


def test_true_http_freezes_distinct_owner_before_queue_and_base_does_not_receive_threads(tmp_path):
    base = _base(tmp_path)
    bind_admin_channel_identity(base.home_paths.root, 'feishu', 'ou_admin')
    paths = gateway_paths(base)
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    ensure_gateway_folders(paths)
    server = GatewayHTTPServer(_free_port(), paths, params=GatewayHTTPServerParams(agent=base,
        auth_middleware=AuthMiddleware(AuthManager(admin_user_id='admin', auth_enabled=True))))
    server.start()
    try:
        for actor, chat, kind in [('ou_one', 'oc_private1', 'p2p'), ('ou_two', 'oc_private2', 'p2p'), ('ou_admin', 'oc_team', 'group')]:
            data = json.dumps({'goal': '合成测试问候', 'conversation_id': chat,
                'metadata': {'message_id': 'om_' + actor, 'channel_chat_type': kind, 'channel_chat_id': chat},
                # 不可信 body owner 字段必须被入口丢弃。
                REQUEST_OWNER_KEY: asdict(OwnerIdentity.local_main())}).encode()
            request = urllib.request.Request(f'http://127.0.0.1:{server.port}/ask', data=data,
                headers={'Content-Type': 'application/json', 'X-Channel': 'feishu', 'X-User-Id': actor,
                         **GatewayClientCredentials(base.home_paths.root).headers()})
            with urllib.request.urlopen(request, timeout=5) as response:
                receipt = json.loads(response.read())
            row = json.loads((paths.inbox/(receipt['request_id']+'.json')).read_text())
            expected = OwnerIdentity.provider_group('feishu', chat) if kind == 'group' else OwnerIdentity.provider_user('feishu', actor)
            assert row[REQUEST_OWNER_KEY] == asdict(expected)
            assert row['owner_id'] == resolve_owner_home(base.home_paths.root, expected).owner_id
            agent = _resolve_request_agent(base, row)
            assert agent.home_paths.owner_id == row['owner_id'] and agent is not base
            assert _process_gateway_request_path(base, paths, paths.inbox/(receipt['request_id']+'.json'), 'owner-test-worker')
            finished = json.loads((paths.done/(receipt['request_id']+'.json')).read_text())
            assert finished['owner_id'] == row['owner_id']
            threads = agent.conversation_store.threads.list()
            assert len(threads) == 1 and threads[0].owner_id == row['owner_id']
        assert not base.conversation_store.threads.list()
    finally:
        server.stop()


def test_frozen_accepted_owner_not_changed_by_later_admin_binding_and_bad_scope_rejected(tmp_path):
    base = _base(tmp_path)
    ordinary = bind_request_owner(base, _payload())
    bind_admin_channel_identity(base.home_paths.root, 'feishu', 'ou_one')
    assert _resolve_request_owner_identity(base, ordinary) == OwnerIdentity.provider_user('feishu', 'ou_one')
    admin = bind_request_owner(base, _payload())
    assert admin[REQUEST_OWNER_KEY] == asdict(OwnerIdentity.local_main())
    remove_admin_channel_identity(base.home_paths.root, 'feishu', 'ou_one')
    assert _resolve_request_owner_identity(base, admin) == OwnerIdentity.local_main()
    with pytest.raises(DataCorruptionError):
        _resolve_request_owner_identity(base, {REQUEST_OWNER_KEY: {'provider': 'feishu', 'owner_kind': 'user', 'owner_id': '../main'}})
    with pytest.raises(DataCorruptionError):
        bind_request_owner(base, {**_payload(), 'metadata': {**_payload()['metadata'], REQUEST_OWNER_KEY: asdict(OwnerIdentity.local_main())}})


def test_adapter_ingress_records_frozen_owner_before_media_gateway_or_reply(tmp_path):
    from types import SimpleNamespace
    base = _base(tmp_path)
    manager = ChannelManager(delivery_state_dir=gateway_paths(base).root/'channel-delivery')
    configure_gateway_client(manager, base)
    manager.register_adapter(SimpleNamespace(adapter_name='feishu', send_message=lambda *_: True))
    for actor, chat, kind in [('ou_one', 'oc_private', 'p2p'), ('ou_two', 'oc_group', 'group')]:
        msg = IncomingMessage(channel='feishu', user_id=actor, message_id='om_'+actor, content='合成内容',
            conversation_id=chat, metadata={'chat_type': kind, 'chat_id': chat})
        assert manager.route_message(msg)
    records = [json.loads(p.read_text()) for p in (gateway_paths(base).root/'channel-delivery').rglob('*.json') if 'records' in p.parts]
    assert len(records) == 2
    assert {r['owner_id'] for r in records} == {'providers/feishu/users/ou_one', 'providers/feishu/groups/oc_group'}
    assert all(r['metadata'][REQUEST_OWNER_KEY]['provider'] == 'feishu' for r in records)


@pytest.mark.parametrize('full', [False, True])
def test_shared_transport_spool_is_hidden_from_admin_and_child_file_shell_tools(tmp_path, full):
    import shlex
    base = _base(tmp_path, full=full)
    source = gateway_paths(base).root/'requests/done/synthetic-foreign.json'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('SYNTHETIC-OTHER-USER-CONTENT')
    root = base.home_paths.owner_home_dir
    policy = PathAccessPolicy.from_values(mode='full' if full else 'normal', owner_scope_root=str(root), agent_home_root=base.home_paths.root)
    assert policy.check(source).code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    assert not execute_registry_test_call(base.tools, 'read_file', {'path': str(source)}, register_with=base).ok
    result = execute_registry_test_call(base.tools, 'run_command', {'command': 'cat '+shlex.quote(str(source))},
        register_with=base)
    assert not result.ok and 'SYNTHETIC-OTHER-USER-CONTENT' not in result.output


def test_group_send_message_and_delegated_origin_use_own_group_never_admin(tmp_path, monkeypatch):
    base = _base(tmp_path)
    bind_admin_channel_identity(base.home_paths.root, 'feishu', 'ou_admin')
    group = _resolve_request_agent(base, _payload('ou_one', 'oc_group', 'group'))
    sent = []
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', lambda method, path, body, token: sent.append((path, body)) or {'code': 0})
    group.channel_registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(proactive=True))
    assert resolve_message_target(group, 'feishu')[1] == 'oc_group'
    result = execute_registry_test_call(group.tools, 'send_message', {'notify_channel': 'feishu', 'message': '合成告警'}, register_with=group)
    assert result.ok, (result.error_code, result.output)
    assert len(sent) == 1 and sent[0][0].endswith('receive_id_type=chat_id') and sent[0][1]['receive_id'] == 'oc_group'
    assert 'ou_admin' not in json.dumps(sent)
    thread = group.conversation_store.threads.get_or_create({**_payload('ou_one', 'oc_group', 'group')['conversation'],
        'owner_id': group.home_paths.owner_id, 'owner_home': str(group.home_paths.owner_home_dir)})
    work = ensure_execution_thread(group, thread.thread_id, 'synthetic-group', kind='delegated')
    dependencies = BackgroundRouteDependencies(group.conversation_store.threads.load,
        lambda: [str(group.home_paths.owner_home_dir)], lambda: ('feishu', 'oc_group'))
    assert resolve_background_route(dependencies, work.thread_id) == ('feishu', 'oc_group')
