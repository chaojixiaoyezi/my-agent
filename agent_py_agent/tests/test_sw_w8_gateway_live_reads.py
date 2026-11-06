"""SW-W8：同实例逐消息/请求读取开关；供应商响应替身不替换入站和业务合同。"""
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.cli.adapter import _register_channel_adapter
from agent_py_agent.cli.space_commands import business_client_for_agent
from agent_py_agent.tests.test_feishu_quoted_media import _event, _manager, _parent, _prepare
from agent_py_agent.tests.test_feishu_quoted_text import _text_parent
from agent_py_agent.tests.test_rework_r8_business_wire_contract import ALICE, thin_client
from agent_py_agent.tests.test_rework_r8_business_wire_contract import gateway as gateway


def _live_adapter(tmp_path, config):
    host = SimpleNamespace(config=config, root=tmp_path,
        home_paths=SimpleNamespace(root=tmp_path / 'home'))
    manager = ChannelManager()
    _register_channel_adapter(manager, 'feishu', host)
    adapter = manager.get_adapter('feishu')
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    return adapter


def _next_event(index, *, private=False, mention='ou_other'):
    event = _event(mention=mention)
    event['event']['message']['message_id'] = f'om_live_{index}'
    if private:
        event['event']['message']['chat_type'] = 'p2p'
    return event


def test_group_mention_flip_changes_next_message_on_same_cli_adapter(tmp_path):
    config = AgentConfig(feishu_session_lock_enabled=False)
    adapter = _live_adapter(tmp_path, config)
    received = []
    adapter.on_message(lambda message: received.append(message) or True)
    for index, (enabled, status) in enumerate(((True, 'ignored'), (False, 'accepted'), (True, 'ignored'))):
        config.feishu_group_require_bot_mention = enabled
        assert adapter._handle_feishu_event(_next_event(index)).status == status
    assert [message.message_id for message in received] == ['om_live_1']


@pytest.mark.parametrize('key,parent,marker', [
    ('feishu_quoted_media_enabled', _parent(), '[已下载到:'),
    ('feishu_quoted_text_enabled', _text_parent('LIVE_QUOTED_TEXT'), '[引用消息资料]'),
])
def test_quote_flip_changes_next_inbound_payload_without_recreating_adapter(tmp_path, monkeypatch, key, parent, marker):
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [parent]}}, enabled=False, text_enabled=False)
    host.config.feishu_session_lock_enabled = False
    host.config.feishu_quoted_media_enabled = host.config.feishu_quoted_text_enabled = False
    _register_channel_adapter(manager, 'feishu', host)
    adapter = manager.get_adapter('feishu')
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    prepared = []
    for index, enabled in enumerate((False, True, False)):
        setattr(host.config, key, enabled)
        event = _next_event(index, mention='ou_bot')
        adapter.on_message(lambda message: prepared.append(_prepare(manager, event)) or True)
        assert adapter._handle_feishu_event(event).status == 'accepted'
        assert (marker in prepared[-1]['prompt']) is enabled
        assert len(gets) == (0 if index == 0 else 1)
    assert len(resources) == (1 if key == 'feishu_quoted_media_enabled' else 0)


@pytest.mark.parametrize('has_home', [True, False])
def test_session_lock_flip_initializes_original_service_or_refuses_private_message(tmp_path, monkeypatch, has_home):
    config = AgentConfig(feishu_session_lock_enabled=False)
    adapter = _live_adapter(tmp_path, config)
    if not has_home:
        adapter.my_agent_home = ''
    received = []
    adapter.on_message(lambda message: received.append(message.message_id) or True)
    monkeypatch.setattr(adapter, '_send_password_card', lambda *args: True)
    assert adapter._handle_feishu_event(_next_event(0, private=True)).status == 'accepted'
    config.feishu_session_lock_enabled = True
    locked = adapter._handle_feishu_event(_next_event(1, private=True))
    assert locked.reason_code == ('FEISHU_UNLOCK_QUEUE_ACCEPTED' if has_home else 'FEISHU_SESSION_LOCK_UNAVAILABLE')
    assert locked.status == ('accepted' if has_home else 'retryable')
    assert received == ['om_live_0']
    assert (adapter._unlock is not None) is has_home
    config.feishu_session_lock_enabled = False
    assert adapter._handle_feishu_event(_next_event(2, private=True)).status == 'accepted'
    assert received == ['om_live_0', 'om_live_2']


# LLM: 两条真实私聊在同一锁首次热开启时交错，队列构造屏障只控制时序，不替换认证与暂存实现。
# 函数用途: 复现半初始化窗口并保证并发消息都被原队列保留。
def test_session_lock_hot_enable_publishes_queue_before_service(tmp_path, monkeypatch):
    config = AgentConfig(feishu_session_lock_enabled=False)
    adapter = _live_adapter(tmp_path, config)
    config.feishu_session_lock_enabled = True
    received, results = [], {}
    adapter.on_message(lambda message: received.append(message) or True)
    monkeypatch.setattr(adapter, '_send_password_card', lambda *args: True)
    constructing, release, second_observed = threading.Event(), threading.Event(), threading.Event()
    original_queue, lock = feishu.FeishuUnlockQueue, threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'live-lock-second':
                second_observed.set()
            lock.acquire()
            return self

        def __exit__(self, *args):
            lock.release()

    def paused_queue():
        constructing.set()
        assert release.wait(5)
        return original_queue()

    def receive(index):
        results[index] = adapter._handle_feishu_event(_next_event(index, private=True))
        if index == 1:
            second_observed.set()

    adapter._lock = ObservedLock()
    monkeypatch.setattr(feishu, 'FeishuUnlockQueue', paused_queue)
    first = threading.Thread(target=receive, args=(0,), name='live-lock-first')
    second = threading.Thread(target=receive, args=(1,), name='live-lock-second')
    first.start()
    try:
        assert constructing.wait(5)
        published_early = adapter._unlock is not None
        second.start()
        assert second_observed.wait(5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not published_early, results
    assert len(results) == 2
    assert all(result.reason_code == 'FEISHU_UNLOCK_QUEUE_ACCEPTED' for result in results.values())
    assert adapter._pending_unlock_messages.depth('ou_user') == 2
    assert received == []


def test_adapter_picks_up_file_switch_change_on_next_message(tmp_path):
    path = tmp_path / 'agent.yaml'

    def write(enabled):
        path.write_text('model_backend: echo\nfeishu_session_lock_enabled: false\n'
            f'feishu_group_require_bot_mention: {str(enabled).lower()}\n', encoding='utf-8')

    write(False)
    adapter = _live_adapter(tmp_path, load_config(path))
    received = []
    adapter.on_message(lambda message: received.append(message.message_id) or True)
    assert adapter._handle_feishu_event(_next_event(0)).status == 'accepted'
    write(True)
    assert adapter._handle_feishu_event(_next_event(1)).status == 'ignored'
    write(False)
    assert adapter._handle_feishu_event(_next_event(2)).status == 'accepted'
    assert received == ['om_live_0', 'om_live_2']


@pytest.mark.parametrize('initial', [False, True])
def test_business_services_follow_live_config_without_reconstruction(tmp_path, initial):
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='local/main'),
        config=AgentConfig(business_spaces_enabled=initial))
    services = api.BusinessServices(host)
    spaces = services.spaces
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    for index, enabled in enumerate((initial, not initial, initial)):
        host.config.business_spaces_enabled = enabled
        status, body = api.execute_business({'action': 'spaces.list', 'request_id': f'live-space-{index}',
            'params': {}}, context, services)
        assert status == (200 if enabled else 503), body
        assert services.spaces is spaces
        assert services.spaces.enabled is enabled
        if enabled:
            assert body['data']['items'] == []
        else:
            assert body['effect_outcome'] == 'not_started'


def test_business_cold_disabled_service_wires_default_ports_after_enable(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    host = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False), tmp_path)
    services = api.BusinessServices(host)
    assert services.background is None and services.projection is None
    host.config.business_spaces_enabled = True
    assert services.ready('background_agents.list')
    assert services.ready('tasks.list')


def test_business_live_switch_roundtrip_uses_real_client_and_gateway(gateway):
    client = business_client_for_agent(thin_client(gateway, ALICE))
    for enabled in (False, True, False, True):
        gateway.agent.config.business_spaces_enabled = enabled
        result = client.call('spaces.list', {})
        assert result['http_status'] == (200 if enabled else 503), result
        assert result['ok'] is enabled
