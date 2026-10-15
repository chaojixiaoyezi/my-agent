"""凭据 URL 是绑定元数据；实际网络调用仍独立守门。"""
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


# LLM: 本夹具显式测task私密卡，活动任务和宿主参数沿原接口；群共享默认与成员事实另有完整矩阵。
# 函数用途: 通过原注册表调用凭据卡，保存真实权限边界用于比较实际网络工具。
def _invoke(agent, params):
    if params.get('operation', 'request') in {'request', 'update'}:
        params = {'access_scope': 'task', **params}
    current = agent._current_run_params
    boundary = write_boundary_with_runtime_ledger(agent, current)
    boundary.update(allow_private_resolution=False, allowed_private_hosts=[], network_access_mode='disabled')
    return execute_registry_test_call(agent.tools, 'request_credentials', params, write_boundary=boundary,
        run_id=current.run_id, call_id=uuid.uuid4().hex, register_with=agent), boundary


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_private_url_binding_can_send_private_card_without_network_authority(tmp_path, kind):
    agent = _agent(tmp_path, kind)
    _thread, actor, _claim = _active(agent)
    sent = []
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda target, card, **kwargs:
        sent.append((target, card)) or {'message_id': 'om_synthetic', 'chat_id': 'oc_private'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    result, boundary = _invoke(agent, {'url': 'https://10.1.2.3/api', 'authentication': 'basic', 'access_scope': 'task'})
    assert result.ok, (result.reported_error_code, result.output)
    assert sent[0][0] == ('oc_synthetic' if kind == 'group' else actor.actor_id)
    blocked = execute_registry_test_call(agent.tools, 'web_fetch', {'url': 'https://10.1.2.3/api'},
        write_boundary=boundary, run_id=agent._current_run_params.run_id, call_id=uuid.uuid4().hex, register_with=agent)
    assert not blocked.ok and not blocked.handler_executed
    assert blocked.reported_error_code == 'NETWORK_ACCESS_DISABLED'


@pytest.mark.parametrize('params', [{'operation':'configure'}, {'operation':'configure','credential_ref':'cred_missing'},
    {'operation':'status'}, {'operation':'update', 'url':'https://example.com', 'authentication':'bearer'}])
def test_missing_conditional_parameters_do_not_enter_handler(tmp_path, params):
    agent = _agent(tmp_path, 'group')
    _active(agent)
    result, _ = _invoke(agent, params)
    assert not result.ok and not result.handler_executed
    assert result.reported_error_code == 'TOOL_INVALID_ARGUMENTS'
    direct = RequestCredentialsTool(agent).execute(params)
    assert not direct.ok and direct.effect_outcome == 'not_started'


def test_unavailable_transport_fails_dependency_without_infinite_pending(tmp_path):
    agent = _agent(tmp_path, 'group')
    _thread, _actor, _claim = _active(agent)
    result, _ = _invoke(agent, {'url':'https://example.com/api', 'authentication':'bearer', 'access_scope':'task'})
    assert not result.ok and result.reported_error_code == 'CHANNEL_ADAPTER_UNAVAILABLE'
    assert result.effect_outcome == 'not_started'
    link = agent.conversation_store.tasks.load(agent._current_run_params.task_id)
    assert not link.input_dependency_refs
