"""普通宿主的只读W6沿业务开关接线，不借未接执行端口开放执行。"""
import json

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import _local_business_services
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.mark.parametrize('enabled', [False, True])
def test_default_readonly_capability_tools_follow_business_switch(tmp_path, monkeypatch, enabled):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home),
        memory_path='memory.jsonl', execution_mode='local_unmanaged', business_spaces_enabled=enabled),
        tmp_path / 'workspace')
    names = agent.tools.tools
    for name in ('business_spaces_capabilities_resolve', 'business_spaces_usage_list'):
        assert (name in names) is enabled
    assert not getattr(agent, '_business_host_ports_wired', False)
    assert getattr(agent, 'workflow_agent_steps', None) is None
    if enabled:
        # 接通工具不等于获得上下文；原无会话准入继续明确拒绝，而非偷偷选空间。
        out = execute_registry_test_call(agent.tools, 'business_spaces_capabilities_resolve', {
            'action': 'spaces.capabilities.resolve', 'params': {'space_id': 'space-not-authorized'},
            'request_id': 'readonly-default-no-context'})
        assert not out.ok
        assert json.loads(out.output)['error']['code'] == 'SPACE_NOT_FOUND'
        assert json.loads(out.output)['effect_outcome'] == 'not_started'
        services = _local_business_services(agent)
        # 显式缺失执行端口依然不可用；只读适配不能填造执行端。
        services.complete_host_wired = False
        services.workflow_port_source = ('explicit', None, None)
        services.workflows.context_provider = None
        services.workflows.engine_factory = None
        assert not services.ready('workflows.run')


def test_session_task_readonly_business_candidates_remain_explicitly_narrowable():
    from agent_py_agent.agent.conversation.background_tool_policy import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )
    names = {'business_spaces_capabilities_resolve', 'business_spaces_usage_list',
             'business_spaces_packages_catalog'}
    default = background_tool_policy_decision(request=BackgroundToolPolicyRequest(reason='session_task'))
    assert names <= set(default.allowed_tools)
    restricted = background_tool_policy_decision(request=BackgroundToolPolicyRequest(
        reason='session_task', policy_snapshot={'allowed_tools': ['skill_search']}))
    assert not names.intersection(restricted.allowed_tools)
    assert set(restricted.allowed_tools) == {'skill_search'}


@pytest.mark.parametrize('mismatch', ['', 'owner', 'task'])
def test_model_business_call_keeps_host_typed_context_and_rejects_identity_conflict(tmp_path, monkeypatch, mismatch):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home),
        execution_mode='local_unmanaged', business_spaces_enabled=True), tmp_path / 'workspace')
    thread = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'frozen-test',
        'channel_user_id': 'local-agent', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    frozen = BusinessExecutionContext('foreign' if mismatch == 'owner' else agent.home_paths.owner_id,
        'space-frozen', None, 'other-task' if mismatch == 'task' else 'stask-frozen', 7, None,
        ('capability:pack',), ('version-asset-frozen',), 'business-context.v1')
    agent._current_run_params = RunParams(request_id='frozen-request', task_id='stask-frozen',
        business_context=frozen, task_attributes={'conversation_thread_id': thread.thread_id})
    if mismatch:
        with pytest.raises(HostAPIError) as caught:
            local_business_context(agent, model=True)
        assert caught.value.code == 'CAPABILITY_DENIED'
    else:
        context, _ = local_business_context(agent, model=True)
        assert context.business_context is frozen
        assert context.business_context.space_revision == 7
        assert context.business_context.asset_version_refs == ('version-asset-frozen',)


@pytest.mark.parametrize('initial', [False, True])
def test_same_services_follow_both_directions_of_live_business_switch(tmp_path, monkeypatch, initial):
    from agent_py_agent.agent.gateway_parts.business_api import BusinessServices

    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home),
        execution_mode='local_unmanaged', business_spaces_enabled=initial), tmp_path / 'workspace')
    services = BusinessServices(agent)
    backend = None
    # 始终使用同一服务对象，不能靠配置缓存重建掩盖冷启动缺口。
    for enabled in (initial, not initial, initial, False, True):
        agent.config.business_spaces_enabled = enabled
        assert services.ready('spaces.capabilities.resolve') is enabled
        if enabled:
            current = services.capabilities
            assert current is not None and current.host is agent
            if backend is None:
                backend = current
            assert current is backend
        else:
            assert services.capabilities is None


@pytest.mark.parametrize('injection', ['argument', 'host', 'setter'])
def test_live_switch_preserves_explicit_capability_port(tmp_path, injection):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.business_api import BusinessServices

    port = SimpleNamespace(execute=lambda action, request: {'action': action})
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/main', owner_home_dir=tmp_path),
        config=SimpleNamespace(business_spaces_enabled=False))
    if injection == 'host':
        host.business_capabilities = port
    services = BusinessServices(host, spaces=SimpleNamespace(enabled=False), workflows=object(),
        capabilities=port if injection == 'argument' else None)
    if injection == 'setter':
        services.capabilities = port
    for enabled in (False, True, False, True):
        host.config.business_spaces_enabled = enabled
        assert services.capabilities is port
        assert services.ready('spaces.capabilities.resolve') is enabled
