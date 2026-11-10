"""新TUI默认只初始化新线程；显式选择、旧值及其它客户端保持原合同。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.verbose import (
    effective_verbose_level,
    initial_verbose_settings,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationLoadRequest,
    _load_gateway_thread,
)
from agent_py_agent.agent.settings import AgentConfig, load_config
from agent_py_agent.cli.chat_parts.tui_actions import _tui_execution_options
from agent_py_agent.cli.chat_parts.tui_input_delivery import TuiActiveInputOutboxEntry

TUI = {'client_capabilities': {'tui_streaming': True, 'rich_transcript': True}}


def test_tui_default_config_and_real_options_are_effective(tmp_path):
    assert AgentConfig().tui_default_verbose_level == 'stream'
    path = tmp_path / 'config.yaml'
    path.write_text('tui_default_verbose_level: off\n')
    assert load_config(path).tui_default_verbose_level == 'off'
    with pytest.raises(ValueError):
        AgentConfig(tui_default_verbose_level='invalid')
    params = SimpleNamespace(runtime_inject=[], prompt_files=[], args=SimpleNamespace())
    options = _tui_execution_options(params)
    assert options.tui_streaming and GatewayAskExecutionOptions.from_payload(options.to_payload()) == options
    entry = TuiActiveInputOutboxEntry('m', 'turn', 'hello', 'hello', execution_options=options)
    assert TuiActiveInputOutboxEntry.from_dict(entry.to_dict()).execution_options == options
    assert not GatewayAskExecutionOptions.from_payload({'client_capabilities': {'rich_transcript': True}}).tui_streaming
    assert not GatewayAskExecutionOptions.from_payload({'client_capabilities': {'tui_streaming': 'true'}}).tui_streaming


@pytest.mark.parametrize('caps', [{}, {'client_capabilities': {'rich_transcript': True}},
                                  {'client_capabilities': {'tui_streaming': 1}}])
def test_other_clients_never_initialize_tui_defaults(caps):
    assert initial_verbose_settings(AgentConfig(), caps) == {}


def test_new_default_legacy_and_manual_choices_survive_restart(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    spec = {'channel': 'chat', 'channel_conversation_id': 'new', 'channel_user_id': 'u', 'canonical_user_id': 'u'}
    agent = SimpleNamespace(config=AgentConfig(), home_paths=None, root=tmp_path, workspace_roots=(tmp_path,))
    from agent_py_agent.agent.gateway_parts import request_context

    # 唯一隔离的是目录投影；线程创建、原锁及持久读取均使用正式实现。
    monkeypatch.setattr(request_context, 'gateway_request_workspace_scope', lambda *_a: (str(tmp_path), (str(tmp_path),)))
    inputs = GatewayConversationLoadRequest(agent, {**TUI, 'conversation': spec}, '', 'hi')
    thread, error = _load_gateway_thread(inputs, store, spec)
    assert not error and thread.verbose_level == 'stream'
    assert effective_verbose_level(thread, TUI) == 'stream'
    assert effective_verbose_level(thread, {}) == 'off'
    assert effective_verbose_level(thread, {'client_capabilities': {'rich_transcript': True}}) == 'off'
    # 同一规范线程绑定IM仍不把客户端默认变为IM过程输出。
    bound = store.threads.bind_channel({'thread_id': thread.thread_id, 'channel': 'feishu', 'channel_conversation_id': 'im', 'channel_user_id': 'u'})
    assert effective_verbose_level(bound, {}) == 'off'
    for level in ['off', 'on', 'full', 'stream']:
        selected = store.threads.update_verbose_level(thread.thread_id, level)
        assert 'verbose_level_source' not in selected.metadata
        cold = ConversationStore(store.storage.root).threads.require(thread.thread_id)
        assert effective_verbose_level(cold, TUI) == effective_verbose_level(cold, {}) == level
        assert store.threads.get_or_create({**spec, **initial_verbose_settings(AgentConfig(), TUI)}).verbose_level == level
    old = store.threads.get_or_create({**spec, 'channel_conversation_id': 'legacy'})
    assert old.verbose_level == 'off' and old.metadata.get('verbose_level_source') is None
    assert store.threads.get_or_create({**spec, 'channel_conversation_id': 'legacy', **initial_verbose_settings(AgentConfig(), TUI)}).verbose_level == 'off'


def test_control_before_first_ask_query_new_and_explicit_off(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        prompt_files=[], gateway_per_user_owner_scoping=False), tmp_path)
    agent.run = lambda *_a, **_k: pytest.fail('control called model')
    scope = control_service.GatewayControlScope('local-agent', 'chat', 'fresh', metadata=TUI)

    def command(text, selected=scope):
        parsed = parse_conversation_control(text)
        result = control_service.execute_gateway_conversation_control(agent, gateway_paths(agent), parsed, selected)
        assert result.ok, result
        return result

    assert '流式' in command('/verbose').message
    thread = agent.conversation_store.threads.resolve(channel='chat', channel_conversation_id='fresh', channel_user_id='local-agent')
    assert thread.verbose_level == 'stream'
    assert '关闭' in command('/verbose', replace(scope, metadata={})).message
    command('/new')
    new = agent.conversation_store.threads.resolve(channel='chat', channel_conversation_id='fresh', channel_user_id='local-agent')
    assert new.thread_id != thread.thread_id and effective_verbose_level(new, TUI) == 'stream'
    command('/verbose off')
    assert '关闭' in command('/verbose').message
    assert '关闭' in command('/verbose', replace(scope, metadata={})).message


def test_tui_model_bootstrap_initializes_default_before_first_ask(tmp_path):
    from agent_py_agent.agent.gateway_parts.model_profile_service import _scoped_model_host
    from agent_py_agent.cli.chat_parts.tui_model_menu import _request_data_sync

    received = []
    def request_models(**kwargs):
        received.append(kwargs)
        return {'ok': True}

    client = SimpleNamespace(gateway_client_only=True, request_models=request_models)
    assert _request_data_sync(client, 'boot', 'list', {})['ok']
    capabilities = received[0]['payload']['metadata']
    assert capabilities == {'client_capabilities': {'tui_streaming': True}}
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        prompt_files=[], gateway_per_user_owner_scoping=False), tmp_path)
    scope = control_service.GatewayControlScope('local-agent', 'chat', 'boot', metadata=capabilities)
    _host, thread = _scoped_model_host(agent, scope)
    assert effective_verbose_level(thread, TUI) == 'stream'
    assert effective_verbose_level(thread, {}) == 'off'
    _host, old = _scoped_model_host(agent, replace(scope, metadata={}))
    assert old.thread_id == thread.thread_id and effective_verbose_level(old, {}) == 'off'
    agent.conversation_store.threads.update_verbose_level(thread.thread_id, 'off')
    _host, selected = _scoped_model_host(agent, scope)
    assert selected.thread_id == thread.thread_id and effective_verbose_level(selected, TUI) == 'off'
