# LLM: 仅离线验证真实 registry、媒体 IR 和三种 provider builder；本地表面相同不能宣称服务端缓存命中。
# 模块用途: 组合已存在的缓存布局、工具连续携带、owner/model 分区和媒体运输，防止旧来源回退主线能力。
import base64
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.cache_diagnostics import (
    compare_request_surfaces,
    request_surface,
)
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.provider_headers import (
    current_provider_session,
    provider_session_scope,
)
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.backends.tool_schema import tool_model_specs_to_anthropic_tools
from agent_py_agent.agent.conversation.input_media import (
    import_input_media,
    input_media_root,
    provider_media_messages,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.agent.tooling.tool_search_state import pending_carried_loaded_tool_names

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


# LLM: 临时 owner/home 与真实 registry；没有 probe/模型调用，图片只经原导入口进入本测试 owner 附件目录。
# 函数用途: 返回稳定工具面与带当前宿主图片的真实原生消息，夹具自身显式隔离日常 home。
@pytest.fixture
def surface_case(tmp_path):
    home = (tmp_path / 'home').resolve()
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), prompt_files=[]), tmp_path)
    assert agent.home_paths.root.resolve() == home
    image_path = tmp_path / 'host-image.png'
    image_path.write_bytes(PNG)
    ref = import_input_media(image_path, input_media_root(agent))
    ir = [RuntimeFactsTurn('当前宿主图片事实', source='host.session_task', media=(ref,)),
          *[UserTurn(f'保留历史条目 {index}') for index in range(32)]]
    canonical = AnthropicMessageAdapter().to_provider_messages(ir)
    messages = provider_media_messages(canonical)
    return agent, ir, canonical, messages


# LLM: carried 只描述展示；每次均取当前 allowed_tools 的真实 runtime snapshot，不让 sticky 名字变成执行权限。
# 函数用途: 按原 registry 和 schema 转换器构造当前可见工具，供三种 provider 使用同一模型面。
def _visible_tools(agent, allowed, records):
    loaded = pending_carried_loaded_tool_names(records)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    specs = agent.tools.model_visible_specs(allowed_tools=allowed, loaded_tool_names=loaded, runtime_snapshot=snapshot)
    return tool_model_specs_to_anthropic_tools(specs)


# LLM: 仅替换末端 request_json；真实 builder 与媒体处理照常执行，owner/thread 原 scope 退出复位。
# 函数用途: 给稳定前缀追加动态尾后生成真实请求体，不发送请求或使用凭据。
def _wire(agent, backend_name, tools, messages, *, stamp='当前时刻一', owner=('local', 'user', 'alice'), model='synthetic-model'):
    config = replace(agent.config, model_backend=backend_name, model_name=model,
        api_base='https://provider.invalid/v1', api_key='synthetic-no-network', stream_enabled=False,
        model_input_modalities=['text', 'image'])
    backend = get_backend(backend_name, config)
    prompt = CacheStructuredPrompt('同一线程的稳定规则与技能索引', volatile_sections=(('host.clock', stamp),))
    sent = []

    def receive(path, payload, headers, **options):
        sent.append(deepcopy(payload))
        if backend_name == 'anthropic_compatible':
            return {'content': [{'type': 'text', 'text': '完成'}], 'stop_reason': 'end_turn', 'usage': {}}
        if backend_name == 'openai_responses':
            return {'status': 'completed', 'output': [{'type': 'message',
                'content': [{'type': 'output_text', 'text': '完成'}]}], 'usage': {}}
        return {'choices': [{'message': {'content': '完成'}, 'finish_reason': 'stop'}], 'usage': {}}

    backend.request_json = receive
    with provider_session_scope(owner, 'same-thread'):
        backend.generate(prompt, tools=tools, messages=messages,
            tool_choice=ToolChoice.auto('ordinary'), request_options=ProviderRequestOptions(system_instruction='固定系统指引'))
    assert len(sent) == 1
    return sent[0], request_surface(sent[0], 'https://provider.invalid/v1')


def _records():
    return [{'tool_round': index, 'tool_result_envelope': {'tool_search': {'loaded_tool_names': names}}}
            for index, names in enumerate((['read_file'], ['list_files'], ['read_file']))]


def _names(tools):
    return [tool.get('name') or tool.get('function', {}).get('name') for tool in tools]


def _image_bytes(value):
    if isinstance(value, dict):
        if value.get('type') == 'image' and value.get('source', {}).get('type') == 'base64':
            yield base64.b64decode(value['source']['data'])
        elif value.get('type') == 'image_url':
            yield base64.b64decode(value['image_url']['url'].split(',', 1)[1])
        elif value.get('type') == 'input_image':
            yield base64.b64decode(value['image_url'].split(',', 1)[1])
        else:
            for item in value.values():
                yield from _image_bytes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _image_bytes(item)


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible', 'openai_responses'])
def test_stable_surface_and_dynamic_tail_preserve_host_image_bytes(surface_case, backend):
    agent, ir, canonical, messages = surface_case
    original = deepcopy(messages)
    records = _records()
    first_tools = _visible_tools(agent, ['read_file', 'list_files'], records)
    second_tools = _visible_tools(agent, ['read_file', 'list_files'], records + [records[-1]])
    first, before = _wire(agent, backend, first_tools, messages)
    second, after = _wire(agent, backend, second_tools, messages, stamp='当前时刻二')
    assert json.dumps(first_tools, ensure_ascii=False) == json.dumps(second_tools, ensure_ascii=False)
    assert before['system'] == after['system'] and before['tools'] == after['tools']
    assert first != second, '必要动态事实必须继续进入真实请求'
    assert list(_image_bytes(first)) == list(_image_bytes(second)) == [PNG]
    assert isinstance(ir[0], RuntimeFactsTurn) and ir[0].media
    assert canonical[0]['content'][-1]['source']['type'] == 'local_file'
    assert messages == original
    diagnostic = compare_request_surfaces(before, after)
    assert diagnostic['server_cache_state'] == 'unknown'
    assert not {'system_changed', 'tools_changed', 'model_changed'} & set(diagnostic['changes'])
    assert '当前宿主图片事实' not in json.dumps(after) and 'synthetic-no-network' not in json.dumps(after)


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible', 'openai_responses'])
def test_sticky_tools_are_removed_from_new_surface_when_current_allowance_changes(surface_case, backend):
    agent, _, _, messages = surface_case
    records = _records()
    before_tools = _visible_tools(agent, ['read_file', 'list_files'], records)
    after_tools = _visible_tools(agent, ['list_files'], records)
    assert 'read_file' in _names(before_tools) and 'read_file' not in _names(after_tools)
    assert 'list_files' in _names(after_tools)
    _, before = _wire(agent, backend, before_tools, messages)
    _, after = _wire(agent, backend, after_tools, messages)
    diagnostic = compare_request_surfaces(before, after)
    assert 'tools_changed' in diagnostic['changes'] and diagnostic['server_cache_state'] == 'unknown'
    assert pending_carried_loaded_tool_names(records) == {'read_file', 'list_files'}


def test_owner_and_model_changes_do_not_reuse_identity_or_cache_observation(surface_case):
    agent, _, _, messages = surface_case
    tools = _visible_tools(agent, ['read_file'], _records())
    first, before = _wire(agent, 'openai_responses', tools, messages)
    replay, same = _wire(agent, 'openai_responses', tools, messages)
    other, _ = _wire(agent, 'openai_responses', tools, messages, owner=('local', 'user', 'bob'))
    changed, after = _wire(agent, 'openai_responses', tools, messages, model='different-model')
    assert first == replay and before == same
    assert first['prompt_cache_key'] != other['prompt_cache_key']
    assert first['model'] != changed['model']
    assert 'model_changed' in compare_request_surfaces(before, after)['changes']
    assert compare_request_surfaces(before, same)['server_cache_state'] == 'unknown'
    assert current_provider_session() == ''
    assert all(name not in first['prompt_cache_key'] for name in ('alice', 'bob', 'synthetic-no-network'))


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible', 'openai_responses'])
def test_tool_description_changes_invalidate_surface_without_renaming_or_hiding_tool(surface_case, backend):
    agent, _, _, messages = surface_case
    original = _visible_tools(agent, ['read_file'], _records())
    changed = deepcopy(original)
    assert changed
    changed[0]['description'] += ' 新的当前说明'
    _, before = _wire(agent, backend, original, messages)
    _, after = _wire(agent, backend, changed, messages)
    assert _names(original) == _names(changed)
    assert 'tools_changed' in compare_request_surfaces(before, after)['changes']
    assert compare_request_surfaces(before, after)['server_cache_state'] == 'unknown'
