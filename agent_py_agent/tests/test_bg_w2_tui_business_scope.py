# LLM: BG-1 客户端侧回归只截获本机请求头，不启动 Gateway、不调用模型；会话作用域只选会话，不携带授权。
# 模块用途: 验证 TUI 团队面板、/space /team /workflow 与原 JSON 传输都带上当前会话 X-Conversation-Id。
from __future__ import annotations

import json
import threading
import urllib.request
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import post_gateway_json
from agent_py_agent.cli.chat_parts import tui_business_spaces
from agent_py_agent.cli.chat_parts.tui_background_agents import start_business_observation
from agent_py_agent.cli.chat_parts.tui_business_spaces import handle_tui_business_command
from agent_py_agent.cli.space_commands import business_client_for_agent


class _Response:
    status = 200

    def __init__(self, body):
        self._body = json.dumps(body).encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append({'scope': request.get_header('X-conversation-id'), 'url': request.full_url,
                     'body': json.loads(request.data.decode('utf-8'))})
        return _Response({'ok': True, 'state': 'completed', 'error': None, 'request_id': 'unit',
                          'data': {'items': [], 'coverage': 'complete', 'next_cursor': None}})

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    return seen


@pytest.fixture
def client_agent(tmp_path):
    config = AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
                         gateway_port=8420, gateway_auth_token='unit-test-token')
    return SimpleNamespace(config=config, owner_identity=None)


def test_post_gateway_json_forwards_only_explicit_conversation_scope(client_agent, captured):
    payload = {'action': 'background_agents.list', 'request_id': 'r1', 'params': {}}
    post_gateway_json(client_agent, '/client/business', payload, timeout=1.0, conversation_id='sess-1')
    post_gateway_json(client_agent, '/client/business', payload, timeout=1.0)
    assert [row['scope'] for row in captured] == ['sess-1', None]
    assert captured[0]['body'] == captured[1]['body'] == payload, '冻结业务正文不得混入身份或会话字段'


def test_business_client_binds_conversation_scope(client_agent, captured):
    business_client_for_agent(client_agent, conversation_id='sess-2').call('background_agents.list', {'limit': 5})
    business_client_for_agent(client_agent).call('background_agents.list', {'limit': 5})
    assert [row['scope'] for row in captured] == ['sess-2', None]


def test_tui_business_command_rebinds_session_scope(client_agent, captured):
    lines = []
    assert handle_tui_business_command('/team list', agent=client_agent, print_line=lines.append, session_id='sess-3')
    first = client_agent._tui_business_command_session
    assert handle_tui_business_command('/team list', agent=client_agent, print_line=lines.append, session_id='sess-3')
    assert client_agent._tui_business_command_session is first
    assert handle_tui_business_command('/team list', agent=client_agent, print_line=lines.append, session_id='sess-4')
    assert client_agent._tui_business_command_session is not first
    assert [row['scope'] for row in captured] == ['sess-3', 'sess-3', 'sess-4']
    assert not handle_tui_business_command('/help', agent=client_agent, print_line=lines.append, session_id='sess-4')


def test_team_panel_observation_uses_root_session(client_agent, captured):
    stop = threading.Event()
    updates = []

    def update(projection):
        updates.append(projection)
        stop.set()

    navigation = SimpleNamespace(root_runtime=SimpleNamespace(session_id='sess-5'), update_business_agents=update)
    start_business_observation(client_agent, navigation, stop).join(timeout=10)
    assert updates and captured and {row['scope'] for row in captured} == {'sess-5'}


def test_tui_command_callsite_passes_current_session(monkeypatch):
    from agent_py_agent.cli.chat_parts.tui import _tui_handle_command
    from agent_py_agent.cli.chat_parts.tui_params import TuiHandleCommandParams

    calls = []
    monkeypatch.setattr(tui_business_spaces, 'handle_tui_business_command',
                        lambda raw, **kwargs: calls.append(kwargs.get('session_id')) or True)
    params = TuiHandleCommandParams(
        user='/team list', agent=SimpleNamespace(), args=SimpleNamespace(), runtime_inject=[], prompt_files=[],
        use_gateway=True, paths=SimpleNamespace(), state_lock=threading.Lock(), is_running_ref=[False],
        pending_jobs_ref=[0], running_prompt_ref=[None], running_request_id_ref=[None],
        running_started_at_ref=[None], shutting_down_ref=[False], stop_event=threading.Event(),
        assistant_outputs=[], current_session_id='sess-6')
    assert _tui_handle_command(params=params) is True
    assert calls == ['sess-6']


def test_plain_chat_business_command_uses_slash_conversation(client_agent, captured):
    from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
    from agent_py_agent.cli.chat_parts.slash_commands import _handle_business_command

    lines = []
    ctx = SlashCommandContext(agent=client_agent, memory_limit=0, runtime_inject=[], prompt_files=[],
                              print_line=lines.append, conversation_id='sess-7', use_gateway=True)
    assert _handle_business_command('/team list', ctx, False) is True
    assert [row['scope'] for row in captured] == ['sess-7']
