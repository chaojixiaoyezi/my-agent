"""真实三协议组包和磁盘历史重载；只捕获出站载荷，不调用供应商。"""
import json
from copy import deepcopy

import pytest

from agent_py_agent.agent.backends.anthropic import (
    AnthropicCompatibleBackend,
    _AnthropicGenerateRequest,
)
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body
from agent_py_agent.agent.backends.openai_chat import (
    OpenAICompatibleBackend,
    _OpenAIGenerateRequest,
)
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)
from agent_py_agent.tests.test_backends_provider_history_reload_identical import (
    _persist_turn,
    _reload_prior_messages,
)


def payload(protocol, messages):
    options = BackendOptions(api_base='https://fixture.invalid', api_key='synthetic-not-a-credential',
        model_name='fixture-model', max_tokens=32)
    tools = [{'name': 'read_file', 'description': '读取', 'input_schema': {
        'type': 'object', 'properties': {'z_path': {'type': 'string'}, 'a_mode': {'type': 'string'}}}}]
    kwargs = dict(prompt='继续', messages=deepcopy(messages), system_instruction='固定规则', tools=tools)
    if protocol == 'anthropic':
        return AnthropicCompatibleBackend(options)._request_payload(_AnthropicGenerateRequest(**kwargs))
    if protocol == 'chat':
        return OpenAICompatibleBackend(options)._request_payload(_OpenAIGenerateRequest(**kwargs))
    backend = OpenAIResponsesBackend(options)
    captured = []

    def capture(actual, _headers, _request):
        captured.append(deepcopy(actual))
        return {'id': 'fixture-response', 'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'ok'}]}], 'usage': {}}

    backend._send_responses_request = capture
    backend._generate(_OpenAIGenerateRequest(**kwargs))
    assert len(captured) == 1
    return captured[0]


def content_strings(value):
    """剥开wire中工具信封的JSON字符串层，逐个核对完整原文而不只检查哨兵。"""
    if isinstance(value, dict):
        for child in value.values():
            yield from content_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from content_strings(child)
    elif isinstance(value, str):
        yield value
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(decoded, (dict, list)):
            yield from content_strings(decoded)


@pytest.mark.parametrize('protocol', ['chat', 'anthropic', 'responses'])
@pytest.mark.parametrize('batch', [32, 64])
def test_complete_tool_batch_wire_bytes_survive_real_disk_reload(tmp_path, protocol, batch):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'now': 1})
    calls = [canonical_history_call('read_file', {'z_path': f'{i}.txt', 'a_mode': 'r'},
        call_id=f'call-{i}') for i in range(batch)]
    results = [canonical_history_result(call, f'BODY-{i}:' + '完整☃结果\n' * 40 + ':END')
        for i, call in enumerate(calls)]
    ir = [UserTurn('完整读取这一批'), AssistantTurn(tool_calls=calls), *results, AssistantTurn(text='完成')]
    live = _persist_turn(store, thread.thread_id, ir)
    restored = _reload_prior_messages(tmp_path, thread.thread_id)
    assert restored == live
    original = deepcopy(live)
    live_payload, reloaded_payload = payload(protocol, live), payload(protocol, restored)
    assert gateway_request_body(live_payload) == gateway_request_body(reloaded_payload)
    assert live == original
    wire = gateway_request_body(live_payload)
    texts = list(content_strings(live_payload))
    for i in range(batch):
        assert f'BODY-{i}:'.encode() in wire
        assert f'call-{i}'.encode() in wire
        assert any(f'BODY-{i}:' + '完整☃结果\n' * 40 + ':END' in text for text in texts)
    assert b':END' in wire
    tool = live_payload['tools'][0]
    shape = tool.get('function', tool)
    schema = shape.get('parameters', shape.get('input_schema'))
    assert list(schema['properties']) == ['z_path', 'a_mode']
