"""自我目录的普通/Compact模型面保持同源；假模型仍走原调用签发和执行器。"""
# ruff: noqa: F811
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_request_projection import tool_loop_prompt_request
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    prepare_conversation_compact_provider_surface,
)
from agent_py_agent.agent.prompting_parts.builder import (
    SELF_MAP_GUIDANCE,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.subagents.capability_scope import bind_creation_tool_authority
from agent_py_agent.agent.tooling.self_map_availability import self_map_available
from agent_py_agent.tests.test_background_main_agent_runtime import _native_probe
from agent_py_agent.tests.test_native_tool_use_ir_messages_flow import _params
from agent_py_agent.tests.test_self_map_runtime import make_agent  # noqa: F401


@pytest.mark.parametrize('enabled,allowed,expected', [
    (True, ['self_map', 'tool_search'], True), (True, ['tool_search'], False),
    (False, ['self_map', 'tool_search'], False),
])
def test_static_guidance_requires_enabled_and_actual_runtime(make_agent, enabled, allowed, expected):
    agent = make_agent(enable_self_map=enabled)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    params = replace(_params(), tool_runtime_snapshot=snapshot, allowed_tools=allowed)
    request = tool_loop_prompt_request(params, runtime_injections=[], workspace_context='', execution_facts='')
    prepared = agent.prompts.prepare_render_input(request)
    assert request.tools.self_map_available is expected
    assert (SELF_MAP_GUIDANCE in prepared.system_prompt) is expected
    assert 'tools' not in prepared.system_prompt.replace(agent.config.system_prompt, '').replace(SELF_MAP_GUIDANCE, '')
    assert ('self_map' in agent.tools.tools) is enabled


def test_closed_switch_also_removes_guidance_from_system_override(make_agent):
    agent = make_agent(enable_self_map=False)
    request = PromptBuildRequest('普通问题', [], system_prompt_override='本次系统规则',
                                 tools=ToolSections(self_map_available=True))
    assert SELF_MAP_GUIDANCE not in agent.prompts.prepare_render_input(request).system_prompt


@pytest.mark.parametrize('enabled', [True, False])
def test_yaml_switch_reaches_runtime_and_cannot_be_self_reenabled(make_agent, tmp_path, enabled):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    path = tmp_path / 'map.yaml'
    path.write_text('model_backend: echo\nenable_self_map: ' + str(enabled).lower() + '\n')
    config = load_config(path)
    config.execution_mode = 'local_unmanaged'
    agent = SimpleAgent(config, tmp_path / 'configured-workspace')
    assert agent.config.enable_self_map is enabled and ('self_map' in agent.tools.tools) is enabled
    before = path.read_bytes()
    denied = execute_registry_test_call(agent.tools, 'user_config',
        {'action': 'set', 'key': 'enable_self_map', 'value': str(not enabled).lower()})
    assert not denied.ok and 'PARAMETER_BOUNDARY' in denied.output
    assert path.read_bytes() == before and agent.config.enable_self_map is enabled


@pytest.mark.parametrize('allowed,expected', [(['self_map', 'tool_search'], True), (['tool_search'], False)])
def test_compact_and_ordinary_have_same_eligible_static_guidance(make_agent, allowed, expected):
    agent = make_agent()
    agent.backend.probe_tool_capability = lambda: _native_probe(agent.backend)
    surface = prepare_conversation_compact_provider_surface(agent,
        ConversationCompactModelSurface(allowed_tools=tuple(allowed)), run_id='fake-compact')
    assert (SELF_MAP_GUIDANCE in surface.stable_prompt_prefix) is expected
    assert ('self_map' in {tool['name'] for tool in surface.tools}) is expected


def test_parent_scope_never_grants_unavailable_self_map(make_agent):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
    from agent_py_agent.agent.agent_core.orchestration.tool_grants import subagent_allowed_tools
    from agent_py_agent.agent.subagents.role_contracts import apply_role_contract_to_create_params
    agent = make_agent()
    for present in (False, True):
        parent = agent.tools.runtime_snapshot(allowed_tools=['list_tools', 'tool_search', 'read_file'] + (['self_map'] if present else []))
        with bind_creation_tool_authority(parent):
            params = create_run_params(agent, {'role': 'worker'}, '读当前范围', subagent_allowed_tools({}))
        params = apply_role_contract_to_create_params(params)
        assert ('self_map' in params.allowed_tools) is present
        assert {'list_tools', 'tool_search'} <= set(params.allowed_tools)
    assert not self_map_available(agent.tools.runtime_snapshot(allowed_tools=['tool_search']))


def test_fake_llm_receives_self_map_owner_facts_and_canonical_history(make_agent):
    agent = make_agent(kind='user')
    class Backend:
        name = 'self-map-native-fake'
        probe_tool_capability = _native_probe
        calls = 0
        observed = ''

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'map-call', 'name': 'self_map', 'input': {'section': 'permissions', 'item': 'current'}}])
            self.observed = json.dumps(kwargs.get('messages') or [], ensure_ascii=False)
            return ModelResponse(text='目录说明当前本人权限；尚未验证真实网络。', backend=self.name)

    backend = Backend()
    agent.backend = backend
    result = agent.run('你现在有哪些权限？', params=RunParams(save=False, request_id='self-map-fake',
        allowed_tools=['self_map', 'tool_search']))
    assert '本人权限' in result.response and backend.calls == 2
    assert 'map-call' in backend.observed and 'self-only' in backend.observed
    assert 'resource_authorization_required' in backend.observed
    assert 'query_evidence.v1' not in backend.observed


def test_replay_same_snapshot_is_deterministic_and_remains_read_only(make_agent):
    agent = make_agent()
    tool = agent.tools.tools['self_map']
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    context = ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=['self_map', 'tool_search', 'read_file']))
    before = tool.execute_scoped({'section': 'tools', 'limit': 1}, context)
    replay = tool.execute_scoped({'section': 'tools', 'limit': 1}, context)
    assert before.ok and replay.output == before.output
    first = json.loads(before.output)
    next_read = first['next_read']
    continued = tool.execute_scoped(next_read['arguments'], context)
    assert continued.ok and json.loads(continued.output)['items'][0]['id'] != first['items'][0]['id']
    assert tool.runtime_policy.effect_resolver.default_effect == 'read_only'
