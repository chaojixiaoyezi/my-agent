"""自身发现走真实执行器、父创建合同和后台目录，只有模型/外部网络使用替身。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.orchestration.tool_grants import subagent_allowed_tools
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.status_projection import status_display_facts
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.capability_scope import bind_creation_tool_authority
from agent_py_agent.agent.subagents.role_contracts import apply_role_contract_to_create_params
from agent_py_agent.agent.subagents.role_templates import (
    DIRECT_CHILD_CONTROL_TOOLS,
    SELF_DISCOVERY_TOOLS,
)
from agent_py_agent.agent.tooling.models import ApprovalPolicy, ToolAvailability
from agent_py_agent.agent.tooling.self_context import permission_self_context
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_background_main_agent_runtime import _native_probe


# LLM: 临时 owner 家目录与真实 registry 隔离生产配置，回调与 composition root 使用相同接口；不调用真实网络。
# 函数用途: 建立可从 Gateway 执行器自查的隔离主代理。
def _agent(tmp_path, *, kind='main'):
    cfg = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
                      my_agent_owner_provider='local' if kind == 'main' else 'feishu',
                      my_agent_owner_kind=kind, my_agent_owner_id='local/main' if kind == 'main' else 'self-only',
                      execution_mode='local_unmanaged')
    agent = SimpleAgent(cfg, tmp_path / 'workspace')
    return agent


# LLM: 只解码真实执行器 handler 的短输出，不以产品内部函数替代 scoped 查询。
# 函数用途: 取得本次 list_tools 实际返回模型的自身事实页。
def _discover(agent, **kwargs):
    result = execute_registry_test_call(agent.tools, 'list_tools', {}, **kwargs)
    assert result.ok, result.output
    return json.loads(result.metadata['handler_details']['tool_output_policy']['live_prompt_output'])


@pytest.mark.parametrize('kind', ['main', 'user', 'group'])
def test_modes_status_and_manifest_share_owner_authority(tmp_path, kind):
    agent = _agent(tmp_path, kind=kind)
    execute_approval_mode_operation(agent.home_paths, 'set', 'ask')
    ask = _discover(agent)['self_context']
    execute_approval_mode_operation(agent.home_paths, 'set', 'auto')
    auto = _discover(agent)['self_context']
    status = status_display_facts(agent)
    assert ask['permissions']['mode'] == 'ask'
    assert auto['permissions'] == status['permissions']
    assert '审批 auto' in status['permission_mode']
    assert auto['permissions']['source'] == 'owner'
    assert auto['identity']['owner_id'] == agent.home_paths.owner_id
    assert auto['identity']['actor'] is None
    encoded = json.dumps(auto)
    assert 'owner_tool_policy_json' not in encoded and 'host_credential_env_names' not in encoded
    assert auto['usage']['help'] == '/help'
    if kind != 'main':
        assert 'admin_controls' not in {item['name'] for item in auto['tool_states']}


def test_invalid_owner_policy_and_scoped_directory_do_not_leak_other_tools(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    execute_approval_mode_operation(agent.home_paths, 'set', 'ask')
    agent.home_paths.owner_tool_policy_json.write_text('{"secret":"MUST-NOT-LEAK"}')
    blocked = execute_registry_test_call(agent.tools, 'list_tools', {})
    assert not blocked.ok
    assert 'APPROVAL_MODE_UNAVAILABLE' in blocked.output
    assert blocked.metadata['action_decision']['evidence']['failure_stage'] == 'authorization'
    assert permission_self_context(agent)['mode'] == 'unavailable'
    agent.home_paths.owner_tool_policy_json.unlink()
    execute_approval_mode_operation(agent.home_paths, 'set', 'ask')
    agent.tools.disabled_tool_names.add('read_file')
    monkeypatch.setattr(agent.tools.tools['write_file'], 'availability',
                        lambda: ToolAvailability(False, 'TOOL_UNAVAILABLE', '尚未配置'))
    page = _discover(agent, allowed_tools=['list_tools', 'read_file', 'write_file'])
    context = page['self_context']
    assert context['permissions']['mode'] == 'ask'
    states = {item['name']: item for item in context['tool_states']}
    assert set(states) == {'list_tools', 'read_file', 'write_file'}
    assert states['read_file']['state'] == 'disabled'
    assert states['write_file']['reason'] == '尚未配置'
    assert 'MUST-NOT-LEAK' not in json.dumps(page)
    assert 'run_command' not in json.dumps(page)


def test_short_manifest_retains_purpose_and_always_grant_contract(tmp_path):
    agent = _agent(tmp_path)
    tool = agent.tools.tools['write_file']
    monkey_policy = replace(tool.runtime_policy,
        approval_policy=ApprovalPolicy('always', always_effects=('dangerous',), owner_grant_parameters=(('path', ('example',)),)))
    tool.runtime_policy = monkey_policy
    page = _discover(agent, allowed_tools=['list_tools', 'write_file'])
    item = next(item for item in page['tools'] if item['name'] == 'write_file')
    assert item['description']
    assert item['approval_policy']['always_effects'] == ['dangerous']
    assert item['approval_policy']['owner_grant_parameters'] == [{'field': 'path', 'values': ['example']}]


def test_worker_creation_intersects_actual_parent_and_keeps_discovery(tmp_path):
    agent = _agent(tmp_path)
    parent_tools = ['list_tools', 'tool_search', 'read_file', 'create_subagents', 'run_command']
    parent = agent.tools.runtime_snapshot(allowed_tools=parent_tools, run_id='parent')
    with bind_creation_tool_authority(parent):
        params = create_run_params(agent, {'goal': '执行已授权任务', 'role': 'worker'}, '执行已授权任务', subagent_allowed_tools({}))
    params = apply_role_contract_to_create_params(params)
    assert set(SELF_DISCOVERY_TOOLS) <= set(params.allowed_tools)
    assert 'read_file' in params.allowed_tools
    assert 'send_message' not in params.allowed_tools and 'feishu_cloud' not in params.allowed_tools
    assert not set(DIRECT_CHILD_CONTROL_TOOLS) & set(params.allowed_tools)
    assert not {'admin_controls', 'restart_gateway', 'user_config'} & set(params.allowed_tools)
    assert set(params.allowed_tools) - {'capability_request', 'record_lesson'} <= set(parent.available_tool_names)


def test_default_worker_inherits_parent_cloud_credentials_message_but_read_only_does_not(tmp_path):
    agent = _agent(tmp_path)
    tools = ['list_tools', 'tool_search', 'read_file', 'request_credentials', 'feishu_cloud', 'send_message']
    # 可用性替身仅代表已配外部依赖，创建与角色合同仍用真实 immutable registry 快照。
    for name in ['request_credentials', 'feishu_cloud', 'send_message']:
        agent.tools.tools[name].availability = lambda: ToolAvailability(True)
    parent = agent.tools.runtime_snapshot(allowed_tools=tools, run_id='parent')
    with bind_creation_tool_authority(parent):
        worker = create_run_params(agent, {'role': 'worker'}, '执行任务', subagent_allowed_tools({}))
        readonly = create_run_params(agent, {'role': 'worker', 'tool_preset': 'read_only'}, '研读任务', subagent_allowed_tools({'tool_preset': 'read_only'}))
    assert {'request_credentials', 'feishu_cloud', 'send_message'} <= set(worker.allowed_tools)
    assert set(SELF_DISCOVERY_TOOLS) <= set(readonly.allowed_tools)
    assert not {'request_credentials', 'feishu_cloud', 'send_message'} & set(readonly.allowed_tools)


def test_fake_llm_uses_real_list_tools_and_receives_owner_facts(tmp_path):
    agent = _agent(tmp_path, kind='user')
    execute_approval_mode_operation(agent.home_paths, 'set', 'ask')

    class Backend:
        name = 'self-discovery-native-test'
        probe_tool_capability = _native_probe
        calls = 0
        observed = ''

        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'self-discovery', 'name': 'list_tools', 'input': {},
                }])
            self.observed = json.dumps(kwargs.get('messages') or [], ensure_ascii=False)
            return ModelResponse(text='当前本人审批模式是 ask，可以查询当前工具用途和权限边界。', backend=self.name)

    backend = Backend()
    agent.backend = backend
    result = agent.run('你目前有哪些能力，为什么有些功能不能用？', params=RunParams(
        save=False, request_id='discovery', allowed_tools=['list_tools', 'tool_search', 'read_file']))
    assert 'ask' in result.response
    assert backend.calls == 2
    assert 'tool-self-context.v1' in backend.observed and 'owner' in backend.observed
    assert 'self-only' in backend.observed


def test_tool_search_reads_exact_ready_tool_schema_without_loading_ungranted_names(tmp_path):
    agent = _agent(tmp_path)
    result = execute_registry_test_call(agent.tools, 'tool_search', {'query': 'read_file'},
                                        allowed_tools=['tool_search', 'read_file'])
    assert result.ok
    payload = json.loads(result.output)
    assert payload['tools'][0]['input_schema']['properties']['path']
    assert payload['loading_scope'] == 'current_thread' and payload['persists_across_compact']
    denied = execute_registry_test_call(agent.tools, 'tool_search', {'query': 'write_file'}, allowed_tools=['tool_search'])
    assert json.loads(denied.output)['tools'] == []


def test_group_actor_and_compact_loaded_state_are_bound_to_current_thread(tmp_path):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor, actor_from_scope
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools

    agent = _agent(tmp_path, kind='group')
    thread = agent.conversation_store.threads.get_or_create({
        'channel': 'feishu', 'channel_conversation_id': 'own-chat', 'channel_user_id': 'actor-A',
        'owner_id': agent.home_paths.owner_id,
    })
    scope = SimpleNamespace(channel='feishu', channel_conversation_id='own-chat', channel_user_id='actor-A')
    actor = actor_from_scope(agent, scope, thread.thread_id, 'request-A')
    params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id}, host_channel_actor=actor)
    agent._current_run_params = params
    persist_discovered_tools(agent, params, {'tool_round': 1, 'tool_result_envelope': {
        'tool_search': {'loaded_tool_names': ['read_file', 'admin_controls']}}})
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda value: replace(value, compact_generation=3))
    facts = _discover(agent, allowed_tools=['list_tools', 'read_file'])['self_context']
    assert facts['identity']['actor']['actor_id'] == 'actor-A'
    assert facts['discovery']['loaded_tools'] == ['read_file']
    assert facts['discovery']['persists_across_compact']
    agent._current_run_params = replace(params, host_channel_actor=HostChannelActor(
        'other-owner', thread.thread_id, 'request-X', 'feishu', 'own-chat', 'actor-X'))
    rejected = _discover(agent, allowed_tools=['list_tools'])['self_context']
    assert rejected['identity']['actor'] is None
    assert rejected['identity']['actor_error_code'] == 'CHANNEL_ACTOR_SCOPE_MISMATCH'


def test_background_defaults_include_self_discovery_but_explicit_scope_stays_exact():
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )

    for reason in ['subagent_runner_finished', 'thread_goal_continue', 'audit_finding', 'scheduled_progress_report']:
        decision = background_tool_policy_decision(request=BackgroundToolPolicyRequest(reason=reason))
        assert {'list_tools', 'tool_search', 'request_credentials', 'feishu_cloud', 'send_message', 'web_fetch'} <= set(decision.allowed_tools)
    configured = background_tool_policy_decision(SimpleNamespace(background_main_agent_allowed_tools=['read_file']))
    assert configured.allowed_tools == ('read_file',)
    unavailable_route = background_tool_policy_decision(request=BackgroundToolPolicyRequest(proactive_delivery_available=False))
    assert 'send_message' not in unavailable_route.allowed_tools


def test_self_context_reports_child_file_and_network_limits(tmp_path):
    agent = _agent(tmp_path)
    agent._current_run_params = RunParams(write_boundary={
        'shell_access_mode': 'restricted', 'read_scope_mode': 'exact', 'allowed_read_roots': [],
        'allowed_write_roots': [], 'effective_permissions': {'network_enabled': False},
    })
    page = _discover(agent, allowed_tools=['list_tools'], write_boundary=agent._current_run_params.write_boundary)['self_context']
    assert page['network']['enabled'] is False
    assert page['filesystem']['shell_access_mode'] == 'restricted'
    # 查询本次 executor 的文件上下文已冻结；状态直接调用时也必须保留显式空根。
    from agent_py_agent.agent.tooling.self_context import tool_self_context
    unscoped = tool_self_context(agent, agent.tools.runtime_snapshot(allowed_tools=['list_tools']))
    assert unscoped['filesystem']['read_roots'] == []
    assert unscoped['filesystem']['write_roots'] == []


def test_scoped_query_uses_actual_invocation_permissions_without_reading_body(tmp_path):
    agent = _agent(tmp_path)
    page = _discover(agent, allowed_tools=['list_tools'], write_boundary={
        'network_access_mode': 'disabled', 'shell_access_mode': 'restricted',
        'network_scope': ['example.invalid'], 'private_host_field': 'NEVER-EXPOSE',
    })['self_context']
    assert page['network']['enabled'] is False
    assert page['network']['scope'] == ['example.invalid']
    assert page['filesystem']['shell_access_mode'] == 'restricted'
    assert 'NEVER-EXPOSE' not in json.dumps(page)
