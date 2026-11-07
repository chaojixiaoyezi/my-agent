"""ACP v1 的纯协议和真实 stdio 往返；不连接真实模型。"""
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces.gap158_w8_acp_protocol import (
    AcpTransport,
    normalize_update,
    read_frame,
)
from agent_py_agent.agent.tooling.mcp_protocol import MCPError


@pytest.mark.parametrize('value', [
    {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '中文\n下一行'}},
    {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': '思考'}},
    {'sessionUpdate': 'tool_call', 'toolCallId': 't'},
    {'sessionUpdate': 'tool_call_update', 'toolCallId': 't'},
    {'sessionUpdate': 'plan', 'entries': []},
    {'sessionUpdate': 'current_mode_update', 'currentModeId': 'code'},
])
def test_notification_normalization(value):
    kind, data = normalize_update(value)
    expected = {'agent_message_chunk': 'agent_message', 'agent_thought_chunk': 'agent_thought',
                'current_mode_update': 'config_state'}
    assert kind == expected.get(value['sessionUpdate'], value['sessionUpdate'])
    assert data == value and data is not value


def test_framing_is_utf8_newline_delimited_and_bounded():
    message = {'jsonrpc': '2.0', 'id': 1, 'result': '中文\n文本'}
    encoded = (json.dumps(message, ensure_ascii=False) + '\n').encode()
    assert read_frame(io.BytesIO(encoded), 1024) == message
    assert read_frame(io.BytesIO(b''), 1024) is None
    for raw in (b'x\n', b'[]\n', b'{"jsonrpc":"2.0","result":NaN}\n', b'x' * 1025 + b'\n'):
        with pytest.raises(MCPError):
            read_frame(io.BytesIO(raw), 1024)
    assert normalize_update({'sessionUpdate': 'unknown_extension'}) is None


@pytest.fixture
def transport(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'gap_w8_fake_acp_agent.py'
    connection = AcpTransport.launch([sys.executable, str(fixture)], cwd=tmp_path,
        environment={'PATH': os.defpath}, timeout=3, max_line_chars=1048576)
    try:
        yield connection
    finally:
        assert connection.terminate(grace_seconds=0.2).confirmed


def test_initialize_and_concurrent_response_correlation(transport):
    assert transport.initialize()['protocolVersion'] == 1
    barrier = threading.Barrier(2)

    def request(value):
        barrier.wait(timeout=2)
        return transport.request('reverse', {'value': value}, timeout=3)

    with ThreadPoolExecutor(2) as executor:
        left, right = executor.submit(request, 1), executor.submit(request, 2)
        assert left.result(5) == {'value': 1}
        assert right.result(5) == {'value': 2}
    assert not transport.inbox.pending and not transport.inbox.responses


def test_unknown_agent_to_client_method_gets_minus_32601(transport):
    seen, event = [], threading.Event()

    def update(params):
        seen.append(params['update'])
        event.set()

    transport.on_update = update
    transport.initialize()
    transport.request('session/new', {'cwd': str(Path.cwd()), 'mcpServers': []}, timeout=3)
    request = {'method': 'unknown/client', 'params': {'sessionId': 'fake-session'}}
    result = transport.request('session/prompt', {'sessionId': 'fake-session',
        'prompt': [{'type': 'text', 'text': 'request:' + json.dumps(request)}]}, timeout=3)
    assert result['stopReason'] == 'end_turn' and event.wait(1)
    reply = json.loads(seen[0]['content']['text'])
    assert reply['id'] == 'client-rpc' and reply['error']['code'] == -32601


def test_remote_error_is_not_a_successful_response(transport):
    transport.initialize()
    with pytest.raises(MCPError) as error:
        transport.request('not-implemented', {}, timeout=3)
    assert error.value.remote_code == -32601
    assert transport.is_running()


def test_launch_refuses_missing_process_identity_before_any_spawn(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_registry
    monkeypatch.setattr(process_registry, 'capture_process_birth_token', lambda pid: '')

    def forbidden(*args, **kwargs):
        raise AssertionError('不能在资源身份观察不可用时启动进程')

    monkeypatch.setattr('subprocess.Popen', forbidden)
    with pytest.raises(MCPError) as error:
        AcpTransport.launch([sys.executable, '-c', 'pass'], cwd=tmp_path, environment={},
                            timeout=1, max_line_chars=1024)
    assert error.value.effect_outcome == 'not_started'


@pytest.mark.parametrize('fields', [
    {'id': True, 'result': {}},
    {'id': False, 'result': {}},
    {'id': 1.5, 'result': {}},
    {'id': {}, 'result': {}},
    {'id': [], 'result': {}},
    {'id': 1, 'method': 4, 'params': {}},
    {'id': 1, 'result': {}, 'error': {'code': -32600, 'message': 'invalid'}},
    {'id': 1, 'error': None},
    {'id': 1, 'error': []},
    {'id': 1, 'error': {}},
    {'id': 1, 'error': {'code': True, 'message': 'invalid'}},
    {'id': 1, 'error': {'code': '-32600', 'message': 'invalid'}},
    {'id': 1, 'error': {'code': -32600}},
    {'id': 1, 'error': {'code': -32600, 'message': None}},
    {'jsonrpc': '2.0'},
])
def test_invalid_rpc_envelope_cannot_alias_a_pending_numeric_id(fields):
    raw = (json.dumps({'jsonrpc': '2.0', **fields}) + '\n').encode()
    with pytest.raises(MCPError) as error:
        read_frame(io.BytesIO(raw), 4096)
    assert error.value.code == 'MCP_PROTOCOL_ERROR'


@pytest.mark.parametrize('identifier', [1, 'request-1', None])
def test_valid_rpc_response_ids_keep_their_wire_type(identifier):
    value = {'jsonrpc': '2.0', 'id': identifier, 'result': {}}
    raw = (json.dumps(value) + '\n').encode()
    assert read_frame(io.BytesIO(raw), 4096) == value
