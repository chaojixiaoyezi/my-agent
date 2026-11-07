"""OpenAI 独立调用的模型目录、亲和、配额、缓冲流及原账本行为回归。"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.provider_headers import _SESSION, current_provider_session
from agent_py_agent.agent.business_spaces import gap158_w1_openai_compat as gateway
from agent_py_agent.agent.business_spaces.gap158_w1_llm_records import make_record_sink
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, ModelCallLedgerOptions
from agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w1 import install_actions
from agent_py_agent.agent.gateway_parts.openai_compat_routes import _send_stream
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.settings.model_scope import (
    ModelScopedAttribute,
    ModelScopeDependencies,
    model_dependencies_scope,
)
from agent_py_agent.agent.user_space.home_layout import home_paths


class FakeOwner:
    config = ModelScopedAttribute('config')
    backend = ModelScopedAttribute('backend')
    prompts = ModelScopedAttribute('prompts')

    def __init__(self, root, paths=None):
        self.home_paths = paths or home_paths(root)
        self.config = AgentConfig(my_agent_home=str(root), business_spaces_enabled=True)
        self.config.openai_compat_gateway_enabled = True
        self.config.llm_call_records_enabled = True
        self.config.my_agent_owner_id = self.home_paths.owner_id
        self.backend = SimpleNamespace(name='deployment')
        self.prompts = SimpleNamespace(config=self.config)
        self._model_call_ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=make_record_sink(self)))


class FakeBackend:
    name = 'fake'
    supports_provider_request_options = True
    stream_enabled = False

    def __init__(self, agent, config, seen):
        self.agent, self.config, self.seen = agent, config, seen

    def generate(self, prompt, *, messages=None, on_chunk=None, request_options=None):
        self.seen.append({'owner': self.agent.home_paths.owner_id, 'model': self.agent.config.model_name,
            'session': current_provider_session(), 'messages': messages, 'prompt': prompt,
            'options': request_options, 'config': self.config})
        hook = getattr(self.agent, 'backend_hook', None)
        if hook:
            hook()
        return ModelResponse('你好，世界。', self.name,
            usage={'prompt_tokens': 7, 'completion_tokens': 4, 'prompt_tokens_details': {'cached_tokens': 2}})


def add_profile(host, name='catalog-model'):
    profile_id = str(uuid4())
    execute_model_profile_operation(host, 'add', {'profile_id': profile_id, 'profile': {
        'model_backend': 'anthropic_compatible', 'model_name': name,
        'api_base': 'https://example.test/anthropic', 'api_key': 'fixture-only',
        'model_context_window_tokens': 96000}})
    execute_model_profile_operation(host, 'set_default', {'profile_id': profile_id})
    return profile_id


@pytest.fixture
def model_host(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import model_scope

    install_actions()
    seen = []
    monkeypatch.setattr(model_scope, '_profile_backend', lambda host, cfg: FakeBackend(host, cfg, seen))
    host = FakeOwner(tmp_path / 'home')
    profile_id = add_profile(host)
    return host, profile_id, seen


def body(model, **extra):
    return {'model': model, 'messages': [{'role': 'system', 'content': 'fixture rule'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}], **extra}


def test_nonstream_shape_usage_and_original_ledger(model_host):
    host, profile_id, seen = model_host
    result = gateway.execute(host, body('catalog-model'), request_id='req-shape')
    assert result['id'] == 'chatcmpl-req-shape' and result['object'] == 'chat.completion'
    assert result['model'] == 'catalog-model' and result['model_profile_id'] == profile_id
    assert result['choices'] == [{'index': 0, 'message': {'role': 'assistant', 'content': '你好，世界。'},
                                  'finish_reason': 'stop'}]
    assert result['usage'] == {'prompt_tokens': 7, 'completion_tokens': 4, 'total_tokens': 11, 'cached_tokens': 2}
    rows = host._model_call_ledger.records()
    assert len(rows) == len(seen) == 1 and rows[0].status == 'finished'
    assert rows[0].metadata['purpose'] == 'openai_compat'
    assert rows[0].accounted_input_tokens == 7 and rows[0].output_tokens == 4
    assert 'hello' not in json.dumps(rows[0].metadata)


def test_context_model_and_session_restore_with_no_affinity_default(model_host):
    host, profile_id, seen = model_host
    outer = replace(host.config, model_name='live-thread')
    dependencies = ModelScopeDependencies(host, (('config', outer), ('backend', host.backend)))
    token = _SESSION.set('a' * 64)
    try:
        with model_dependencies_scope(host, dependencies):
            gateway.execute(host, body(profile_id))
            assert host.config.model_name == 'live-thread'
            assert current_provider_session() == 'a' * 64
    finally:
        _SESSION.reset(token)
    assert seen[0]['model'] == 'catalog-model' and seen[0]['session'] == ''
    assert host.config.model_name != 'live-thread'


def test_affinity_owner_scoped_and_config_overrides_local(model_host):
    host, profile_id, seen = model_host
    old = host.config
    gateway.execute(host, body(profile_id, max_tokens=9, temperature=0.6, top_p=0.8), {'X-Session-Id': 'client-one'})
    assert seen[0]['session'] == gateway.affinity_key(host.home_paths.owner_id, 'client-one')
    assert seen[0]['session'] != gateway.affinity_key('another-owner', 'client-one')
    assert seen[0]['options'].max_output_tokens == 9
    assert seen[0]['config'].temperature == 0.6 and seen[0]['config'].top_p == 0.8
    assert host.config is old
    with pytest.raises(HostAPIError):
        gateway.execute(host, body(profile_id), {'X-Session-Id': 'x' * 129})
    assert len(seen) == 1


@pytest.mark.parametrize('extra', [{'stream': True, 'n': 2}, {'stream': True, 'tools': []},
                                  {'stream': True, 'functions': []}, {'owner_id': 'foreign'},
                                  {'messages': []}, {'stream': 'true'}])
def test_invalid_input_rejected_before_any_model_call(model_host, extra):
    host, profile_id, seen = model_host
    with pytest.raises(HostAPIError) as error:
        gateway.execute(host, body(profile_id, **extra))
    assert error.value.code == 'INVALID_SCHEMA' and not seen and host._model_call_ledger.records() == ()


def test_unknown_and_foreign_profile_not_resolved(model_host, tmp_path):
    host, _, seen = model_host
    other = FakeOwner(tmp_path / 'other-home')
    foreign = add_profile(other, 'foreign-only')
    for model in ('raw-provider-string', foreign, 'foreign-only'):
        with pytest.raises(HostAPIError) as error:
            gateway.execute(host, body(model))
        assert error.value.code == 'RESOURCE_NOT_FOUND' and error.value.status == 404
    assert not seen


def test_inflight_limit_and_failure_release_are_original_admission(model_host):
    host, profile_id, seen = model_host
    entered, release = threading.Event(), threading.Event()
    host.config.gateway_user_inflight_limit = 1
    host.backend_hook = lambda: (entered.set(), release.wait(3))
    errors = []

    def first():
        try:
            gateway.execute(host, body(profile_id), admission_key='quota-fixture')
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(HostAPIError) as error:
            gateway.execute(host, body(profile_id), admission_key='quota-fixture')
        assert error.value.code == 'RATE_LIMITED' and error.value.status == 429
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and len(seen) == 1
    del host.backend_hook
    with pytest.raises(HostAPIError):
        gateway.execute(host, body('unknown'), admission_key='quota-fixture')
    gateway.execute(host, body(profile_id), admission_key='quota-fixture')
    assert len(seen) == 2


def test_buffered_sse_order_chunk_bound_and_disconnect(model_host, monkeypatch):
    host, profile_id, seen = model_host
    result = gateway.execute(host, body(profile_id, stream=True))
    monkeypatch.setattr(gateway, 'OPENAI_COMPAT_STREAM_CHUNK_CHARS', 2)
    frames = list(gateway.stream_frames(result))
    assert frames[-1] == b'data: [DONE]\n\n'
    chunks = [json.loads(frame.decode()[6:]) for frame in frames[:-1]]
    assert all(chunk['object'] == 'chat.completion.chunk' for chunk in chunks)
    assert chunks[0]['choices'][0]['delta'] == {'role': 'assistant'}
    content = [chunk['choices'][0]['delta']['content'] for chunk in chunks[1:-1]]
    assert ''.join(content) == '你好，世界。' and all(len(part) <= 2 for part in content)
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'

    def disconnected(_frame):
        raise BrokenPipeError()

    handler = SimpleNamespace(send_response=lambda _: None, send_header=lambda *_: None,
        end_headers=lambda: None, wfile=SimpleNamespace(write=disconnected, flush=lambda: None))
    _send_stream(handler, result)
    assert handler.close_connection and len(seen) == len(host._model_call_ledger.records()) == 1


def test_tiny_test_uses_same_call_and_output_budget(model_host):
    host, profile_id, seen = model_host
    result = gateway.test_gateway(host, {'model_profile_id': profile_id})
    assert result['ok'] and result['model_profile_id'] == profile_id
    assert seen[0]['options'].max_output_tokens == gateway.LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS
    assert len(host._model_call_ledger.records()) == 1
    assert 'Reply OK.' not in json.dumps(host._model_call_ledger.records()[0].metadata)


@pytest.mark.parametrize('testing', [False, True])
@pytest.mark.parametrize('affinity', ['', 'client-session'])
def test_configured_session_header_reaches_real_http_assembly(model_host, monkeypatch, testing, affinity):
    """复用实际 HTTP 信封和头组装；只看 ContextVar 会漏掉无亲和时的发送失败。"""
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.settings import model_scope

    host, profile_id, seen = model_host
    original_backends, requests = [], []

    class HeaderBackend(FakeBackend):
        def __init__(self, agent, config, seen):
            super().__init__(agent, config, seen)
            self.api_base, self.api_key = config.api_base, config.api_key
            self.request_timeout, self.connect_timeout = 10., 5.
            self.custom_headers = {'X-Custom': 'kept'}
            self.session_header = 'X-Provider-Session'
            original_backends.append(self)

        def generate(self, prompt, *, messages=None, on_chunk=None, request_options=None):
            requests.append(HttpBackend._gateway_request(self, '/v1/chat/completions',
                {'messages': messages}, {'Authorization': 'Bearer fixture-only'}))
            return super().generate(prompt, messages=messages, on_chunk=on_chunk, request_options=request_options)

    monkeypatch.setattr(model_scope, '_profile_backend', lambda agent, config: HeaderBackend(agent, config, seen))
    headers = {'X-Session-Id': affinity} if affinity else {}
    token = _SESSION.set('outer-live-thread')
    try:
        if testing:
            result = gateway.test_gateway(host, {'model_profile_id': profile_id}, headers=headers)
            assert result['ok'] is True
        else:
            result = gateway.execute(host, body(profile_id), headers)
            assert result['choices'][0]['message']['content'] == '你好，世界。'
        assert current_provider_session() == 'outer-live-thread'
    finally:
        _SESSION.reset(token)
    assert len(requests) == len(seen) == len(host._model_call_ledger.records()) == 1
    assert requests[0].headers['X-Custom'] == 'kept'
    if affinity:
        assert requests[0].headers['X-Provider-Session'] == gateway.affinity_key(host.home_paths.owner_id, affinity)
    else:
        assert 'X-Provider-Session' not in requests[0].headers
    assert all(backend.session_header == 'X-Provider-Session' for backend in original_backends)
