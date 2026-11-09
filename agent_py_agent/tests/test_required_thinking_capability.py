"""必需思考与effort/工具选择分离；真实回环HTTP核对出站，不请求真实供应商。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions, ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderConfigurationError
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.backends.model_request_capabilities import (
    ModelRequestCapabilities,
    resolve_model_request_capabilities,
)
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.reasoning_effort import request_reasoning_options
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

TOOLS = [{'name': 'record', 'description': 'synthetic tool', 'input_schema': {'type': 'object', 'properties': {}}}]


def _options(backend, *, forced, level='low'):
    agent = SimpleNamespace(config=SimpleNamespace(model_reasoning_effort=level))
    disabled, effort = request_reasoning_options(agent, None, backend, forced=forced)
    return ProviderRequestOptions(thinking_disabled=disabled, reasoning_effort=effort)


def test_existing_minimax_profile_cannot_force_thinking_disabled():
    config = AgentConfig()
    config.api_base, config.api_key = 'https://api.minimaxi.com/anthropic', 'synthetic-key'
    config.model_name, config.model_reasoning_control = 'MiniMax-M3.1-Flash', 'effort'
    config.model_reasoning_levels = ['low', 'medium', 'high', 'xhigh', 'max']
    backend = get_backend('anthropic_compatible', config)
    payload = backend.project_generate_payload('synthetic', tools=TOOLS, tool_choice=ToolChoice.none(),
                                               request_options=_options(backend, forced=True))
    assert payload['thinking'] == {'type': 'adaptive'}
    assert payload['output_config'] == {'effort': 'low'}
    assert 'tools' not in payload and 'tool_choice' not in payload


@contextmanager
def _provider():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            if self.path.endswith('/messages'):
                content = [{'type': 'text', 'text': 'ok'}]
                if any(t.get('name') == 'my_agent_structured_output' for t in payload.get('tools', [])):
                    content = [{'type': 'tool_use', 'id': 'json', 'name': 'my_agent_structured_output', 'input': {'ok': True}}]
                data = {'content': content, 'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}}
            elif self.path.endswith('/responses'):
                data = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'ok'}]}], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
            else:
                data = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
            invalid = payload.get('thinking') == {'type': 'disabled'} or payload.get('reasoning', {}).get('effort') == 'none'
            raw = json.dumps({'error': {'message': 'synthetic invalid control'}} if invalid else data).encode()
            self.send_response(400 if invalid else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize('backend_class', [AnthropicCompatibleBackend, OpenAICompatibleBackend, OpenAIResponsesBackend])
@pytest.mark.parametrize('choice', [ToolChoice.auto(), ToolChoice.none(), ToolChoice.specific('record')])
def test_declared_required_thinking_wire_preserves_effort_and_valid_choices(backend_class, choice):
    with _provider() as (url, calls):
        backend = backend_class(BackendOptions(url, 'synthetic-key', 'opaque-model', stream_enabled=False,
            thinking_requirement='required_adaptive', reasoning_control='effort', reasoning_levels=('low', 'high')))
        response = backend.generate('synthetic', tools=TOOLS, tool_choice=choice,
                                    request_options=_options(backend, forced=choice.mode != 'auto'))
        assert response.text == 'ok' and len(calls) == 1
        payload = calls[0]
        if backend_class is AnthropicCompatibleBackend:
            assert payload['thinking'] == {'type': 'adaptive'} and payload['output_config'] == {'effort': 'low'}
        elif backend_class is OpenAICompatibleBackend:
            assert payload['thinking'] == {'type': 'adaptive'} and payload['reasoning_effort'] == 'low'
        else:
            assert 'thinking' not in payload and payload['reasoning'] == {'effort': 'low'}


def test_auto_only_provider_none_is_equivalent_and_forced_is_locally_rejected():
    with _provider() as (url, calls):
        backend = AnthropicCompatibleBackend(BackendOptions(url, 'synthetic-key', 'opaque-model', stream_enabled=False,
            thinking_requirement='required_adaptive', reasoning_control='effort'))
        backend.model_request_capabilities = ModelRequestCapabilities('required_adaptive', ('auto',))
        history = [
            {'role': 'user', 'content': 'synthetic'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'prior', 'name': 'record', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'prior', 'content': 'synthetic result'}]},
        ]
        backend.generate('synthetic', messages=history, tools=TOOLS, tool_choice=ToolChoice.none(),
                         request_options=_options(backend, forced=True))
        assert len(calls) == 1 and 'tools' not in calls[0] and 'tool_choice' not in calls[0]
        blocks = [block for message in calls[0]['messages'] for block in message['content']
                  if isinstance(message['content'], list) and isinstance(block, dict)]
        assert any(block.get('type') == 'tool_use' and block.get('id') == 'prior' for block in blocks)
        assert any(block.get('type') == 'tool_result' and block.get('tool_use_id') == 'prior' for block in blocks)
        for choice in (ToolChoice.required(), ToolChoice.specific('record')):
            with pytest.raises(ProviderConfigurationError) as caught:
                backend.generate('synthetic', tools=TOOLS, tool_choice=choice, request_options=_options(backend, forced=True))
            assert caught.value.reason == 'forced_tool_choice_not_supported'
        # 原structured信封依赖强制tool；不静默降auto，也不把普通正文当已验证JSON。
        with pytest.raises(ProviderConfigurationError):
            backend.generate_structured('synthetic', response_schema={'type': 'object'})
        assert len(calls) == 1


@pytest.mark.parametrize('level', ['off', 'none', 'minimal'])
def test_explicit_disable_is_local_error_and_does_not_send(level):
    with _provider() as (url, calls):
        backend = AnthropicCompatibleBackend(BackendOptions(url, 'synthetic-key', 'opaque-model', stream_enabled=False,
            thinking_requirement='required_adaptive', reasoning_control='effort'))
        with pytest.raises(ProviderConfigurationError):
            backend.generate('synthetic', request_options=ProviderRequestOptions(reasoning_effort=level))
        with pytest.raises(ProviderConfigurationError):
            backend.generate('synthetic', request_options=ProviderRequestOptions(thinking_disabled=True))
        assert calls == []


def test_required_thinking_structured_wire_when_provider_supports_specific():
    with _provider() as (url, calls):
        backend = AnthropicCompatibleBackend(BackendOptions(url, 'synthetic-key', 'opaque-model', stream_enabled=False,
            thinking_requirement='required_adaptive', reasoning_control='effort', reasoning_levels=('low',)))
        result = backend.generate_structured('synthetic', response_schema={'type': 'object'},
                                            request_options=_options(backend, forced=False))
        assert json.loads(result.text) == {'ok': True} and len(calls) == 1
        assert calls[0]['thinking'] == {'type': 'adaptive'} and calls[0]['output_config'] == {'effort': 'low'}


@pytest.mark.parametrize('host,model,required', [
    ('api.minimaxi.com', 'MiniMax-M3.1-Flash', True),
    ('api.minimax.io', 'MiniMax-M3.1-Flash', True),
    ('api.minimax.cn', 'MiniMax-M3.1-Flash', True),
    ('api.minimaxi.com.attacker.invalid', 'MiniMax-M3.1-Flash', False),
    ('api.minimaxi.com', 'MiniMax-M3.1-Flash-other', False),
    ('api.minimaxi.com', 'minimax-m3.1-flash', False),
    ('api.minimaxi.com', 'MiniMax-M3', False),
])
def test_verified_defaults_use_exact_endpoint_and_model_identity(host, model, required):
    cap = resolve_model_request_capabilities('auto', f'https://{host}/anthropic', model, 'anthropic_compatible')
    assert (cap.thinking_requirement == 'required_adaptive') is required


def test_profile_requirement_roundtrip_and_other_profile_reset(tmp_path):
    from agent_py_agent.agent.settings.model_profiles import (
        execute_model_profile_operation,
        selected_model_config,
    )
    from agent_py_agent.agent.settings.model_provider_schema import (
        ModelProfileError,
        validate_model,
    )
    from agent_py_agent.tests.test_model_profiles import Host, add

    host = Host(tmp_path)
    key, result = add(host, thinking_requirement='required_adaptive', reasoning_control='effort')
    plain, _ = add(host)
    assert result['ok']
    row = next(item for item in execute_model_profile_operation(host, 'list', {})['profiles'] if item['id'] == key)
    assert row['thinking_requirement'] == 'required_adaptive'
    assert selected_model_config(host, profile_id=key).model_thinking_requirement == 'required_adaptive'
    assert selected_model_config(host, profile_id=plain).model_thinking_requirement == 'auto'
    with pytest.raises(ModelProfileError):
        validate_model({'model_backend': 'anthropic_compatible', 'model_name': 'opaque', 'provider_id': 'p',
                        'thinking_requirement': 'guess'})


def test_explicit_optional_and_budget_conflicts_are_local_and_other_models_keep_forced_semantics():
    with pytest.raises(ProviderConfigurationError) as conflict:
        AnthropicCompatibleBackend(BackendOptions('https://api.minimaxi.com/anthropic', 'synthetic', 'MiniMax-M3.1-Flash',
                                                 thinking_requirement='optional'))
    assert conflict.value.reason == 'thinking_requirement_conflict'
    with pytest.raises(ProviderConfigurationError) as budget:
        AnthropicCompatibleBackend(BackendOptions('https://example.invalid', 'synthetic', 'opaque',
                                                 thinking_requirement='required_adaptive', reasoning_control='budget'))
    assert budget.value.reason == 'adaptive_budget_not_supported'
    ordinary = AnthropicCompatibleBackend(BackendOptions('https://example.invalid', 'synthetic', 'opaque',
                                                         reasoning_control='effort'))
    payload = ordinary.project_generate_payload('synthetic', tools=TOOLS, tool_choice=ToolChoice.specific('record'),
                                                request_options=_options(ordinary, forced=True))
    assert payload['thinking'] == {'type': 'disabled'} and payload['tool_choice'] == {'type': 'tool', 'name': 'record'}
    required = AnthropicCompatibleBackend(BackendOptions('https://example.invalid', 'synthetic', 'opaque',
                                                         thinking_requirement='required_adaptive', reasoning_control='effort'))
    with pytest.raises(ProviderConfigurationError):
        _options(required, forced=True, level='off')



def test_tui_edit_preserves_explicit_thinking_capability():
    from agent_py_agent.cli.chat_parts.tui_provider_menu import _GenerationFields

    form = _GenerationFields({'thinking_requirement': 'required_adaptive'}, 'anthropic_compatible')
    assert form.values()['thinking_requirement'] == 'required_adaptive'
