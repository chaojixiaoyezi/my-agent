"""凭据发现只返回原宿主裁决；原工具schema/Registry与合成owner，不读取真实凭据。"""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.tooling.models import tool_effect_for_runtime_policy
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool


@pytest.mark.parametrize('params,valid', [
    ({'operation': 'list'}, True),
    ({'operation': 'share', 'credential_ref': 'cred_synthetic'}, True),
    ({'operation': 'share'}, False),
    ({'operation': 'find', 'url': 'https://example.com/api'}, True),
    ({'operation': 'find', 'url': 'https://example.com/api', 'credential_name': '站点A'}, True),
    ({'operation': 'find'}, False), ({'operation': 'find', 'url': ''}, False),
    ({'operation': 'request', 'access_scope': 'group'}, True),
    ({'operation': 'request', 'access_scope': 'task'}, True),
    ({'operation': 'request', 'access_scope': 'tenant'}, False),
])
def test_discovery_schema_conditions_are_explicit(params, valid):
    tool = RequestCredentialsTool(SimpleNamespace())
    assert validate_tool_input(params, tool.model_spec.input_schema).ok is valid


@pytest.mark.parametrize('operation', ['list', 'find'])
def test_discovery_only_declares_read_only_effect(operation):
    policy = RequestCredentialsTool(SimpleNamespace()).runtime_policy
    assert tool_effect_for_runtime_policy(policy, {'operation': operation}) == 'read_only'
    assert tool_effect_for_runtime_policy(policy, {'operation': 'request'}) == 'mutating'
    assert tool_effect_for_runtime_policy(policy, {'operation': 'revoke'}) == 'mutating'
    assert tool_effect_for_runtime_policy(policy, {'operation': 'share'}) == 'mutating'


# LLM: 假helper仅检验工具参数与安全结果不被二次选账号；真实存储/成员/跨任务复用另由集成矩阵验证。
# 函数用途: 在helper尚未实现时先固定工具公开接缝，不向测试模型提供任何凭据明文。
@pytest.fixture
def discovery_tool(monkeypatch):
    calls = []
    result = {'credentials': [{'credential_ref': 'cred_shared'}, {'credential_ref': 'cred_own'}],
        'selected_credential_ref': 'cred_own', 'selection_reason': 'own_ready', 'own_credential_present': True}
    def discover(agent, *, url='', name='', operation='list'):
        calls.append((agent, url, name, operation))
        return result
    module = ModuleType('agent_py_agent.agent.user_space.task_credential_groups')
    module.discover_group_credentials = discover
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
        lambda agent: SimpleNamespace(im_task_credentials_enabled=True))
    return RequestCredentialsTool(SimpleNamespace()), calls, result


@pytest.mark.parametrize('operation', ['list', 'find'])
def test_tool_preserves_host_selection_without_sorting_or_fallback(discovery_tool, operation):
    tool, calls, expected = discovery_tool
    result = tool.execute({'operation': operation, 'url': 'https://example.com/api', 'credential_name': '站点A'})
    assert result.ok
    assert calls == [(tool.agent, 'https://example.com/api', '站点A', operation)]
    assert json.loads(result.output) == expected
    assert result.result_envelope['credential_discovery'] == expected


def test_own_failed_candidate_does_not_become_other_shared_account(discovery_tool):
    tool, calls, expected = discovery_tool
    expected.update(selected_credential_ref='', selection_reason='own_unavailable')
    result = tool.execute({'operation': 'find', 'url': 'https://example.com/api'})
    assert result.ok and len(calls) == 1
    assert json.loads(result.output)['selected_credential_ref'] == ''
    assert json.loads(result.output)['own_credential_present'] is True


def test_explicit_request_never_implicitly_reuses_someone_elses_reference(discovery_tool, monkeypatch):
    tool, calls, _ = discovery_tool
    requests = []
    def request(*args, **kwargs):
        requests.append(kwargs)
        raise ValueError('synthetic stop before send')
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.request_credential_card', request)
    result = tool.execute({'operation': 'request', 'url': 'https://example.com/api'})
    assert not result.ok and len(requests) == 1 and calls == []


def test_model_hints_describe_discovery_own_priority_and_no_failure_switch():
    tool = RequestCredentialsTool(SimpleNamespace())
    hints = tool.model_spec.hints
    assert hints.use_cases and hints.keywords
    assert any('find' in text for text in hints.use_cases)
    description = tool.model_spec.description
    assert '403' in description and '401' in description and 'request' in description


@pytest.mark.parametrize('operation', ['list', 'find'])
def test_original_registry_forwards_discovery_without_waiting_input(tmp_path, monkeypatch, discovery_tool, operation):
    from agent_py_agent.tests.test_im_credential_card_preflight import _invoke
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    _tool, calls, expected = discovery_tool
    agent = _agent(tmp_path, 'group')
    _active(agent)
    before = agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs
    result, _boundary = _invoke(agent, {'operation': operation, 'url': 'https://example.com/api'})
    assert result.ok and result.handler_executed
    assert result.metadata['handler_details']['credential_discovery'] == expected
    assert calls == [(agent, 'https://example.com/api', '', operation)]
    assert agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs == before == ()


def test_original_tool_search_exposes_new_operations_only_with_current_authority(tmp_path):
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    agent = _agent(tmp_path, 'group')
    _active(agent)
    current = agent._current_run_params
    found = execute_registry_test_call(agent.tools, 'tool_search', {'query': 'request_credentials'},
        run_id=current.run_id, register_with=agent,
        allowed_tools=['tool_search', 'request_credentials'])
    assert found.ok
    payload = json.loads(found.output)
    schema = payload['tools'][0]['input_schema']
    assert {'list', 'find'} <= set(schema['properties']['operation']['enum'])
    assert set(schema['properties']['access_scope']['enum']) == {'task', 'group'}
    hidden = execute_registry_test_call(agent.tools, 'tool_search', {'query': 'request_credentials'},
        run_id=current.run_id, call_id='hidden-discovery', register_with=agent, allowed_tools=['tool_search'])
    assert hidden.ok and json.loads(hidden.output)['tools'] == []


def test_fake_native_llm_discovers_then_queries_without_input_wait_or_new_card(tmp_path, discovery_tool):
    from dataclasses import replace

    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.test_background_main_agent_runtime import _native_probe
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    _tool, calls, expected = discovery_tool
    agent = _agent(tmp_path, 'group')
    _active(agent)
    class Backend:
        name = 'synthetic-credential-discovery'
        probe_tool_capability = _native_probe
        calls = 0
        observed = ''
        def generate(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[
                    {'id': 'search', 'name': 'tool_search', 'input': {'query': 'request_credentials'}}])
            if self.calls == 2:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[
                    {'id': 'find', 'name': 'request_credentials', 'input': {'operation': 'find', 'url': 'https://example.com/api'}}])
            self.observed = json.dumps(kwargs.get('messages') or [], ensure_ascii=False)
            return ModelResponse(text='已找到当前可用账号，没有申请新卡。', backend=self.name)
    backend = Backend()
    agent.backend = backend
    result = agent.run('看看群里已存的站点账号，不要发新卡。', params=replace(agent._current_run_params,
        save=False, allowed_tools=['tool_search', 'request_credentials']))
    assert result.response and backend.calls == 3
    assert calls == [(agent, 'https://example.com/api', '', 'find')]
    assert expected['selected_credential_ref'] in backend.observed
    assert not agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs


# LLM: 使用作者原临时群卡/成员fixture与真实Registry/HTTP凭据handler，只替换最末HTTP，不得手造秘密授权或跨owner路径。
# 函数用途: 从工具发现返回的准确引用执行同源请求，记录实际使用账号而不把秘密交模型。
def _fetch_with_reference(ctx, reference):
    import uuid

    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    current = ctx.agent._current_run_params
    return execute_registry_test_call(ctx.agent.tools, 'web_fetch',
        {'url': 'https://example.com/api', 'credential_ref': reference, 'mode': 'text'},
        write_boundary=write_boundary_with_runtime_ledger(ctx.agent, current),
        run_id=current.run_id, call_id=uuid.uuid4().hex, register_with=ctx.agent)


def test_real_group_new_member_request_finds_and_uses_original_shared_reference(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.tests.test_group_shared_credentials import (
        _group_case,
        _issue,
        _switch_actor,
    )
    ctx = _group_case(tmp_path)
    reference = _issue(ctx, submitter='ou_actor_a')
    sent_before = len(ctx.sent)
    _switch_actor(ctx, 'ou_actor_c')
    found, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    assert found.ok, (found.error_code, found.output)
    discovery = found.metadata['handler_details']['credential_discovery']
    assert discovery['selected_credential_ref'] == reference
    assert not discovery['own_credential_present']
    sends = []
    def send(request, **kwargs):
        sends.append(request.headers['Authorization'])
        return RawResponseParts(200, {}, b'{"alive":true}', request.url)
    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', send)
    result = _fetch_with_reference(ctx, discovery['selected_credential_ref'])
    assert result.ok, (result.error_code, result.output)
    assert sends == ['Bearer SyntheticGroupSecret-ou_actor_a']
    assert len(ctx.sent) == sent_before
    assert 'SyntheticGroupSecret' not in found.output + result.output
    assert 'SyntheticPrivateNote' not in found.output
    link = ctx.agent.conversation_store.tasks.load(ctx.agent._current_run_params.task_id)
    assert not link.input_dependency_refs


@pytest.mark.parametrize('status', [401, 403])
def test_real_own_failure_never_switches_until_explicit_revoke_then_find_uses_a(tmp_path, monkeypatch, status):
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.tests.test_group_shared_credentials import _group_case, _issue
    ctx = _group_case(tmp_path)
    a = _issue(ctx, submitter='ou_actor_a')
    b = _issue(ctx, submitter='ou_actor_b')
    sent_before = len(ctx.sent)
    selected, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    assert selected.ok
    assert selected.metadata['handler_details']['credential_discovery']['selected_credential_ref'] == b
    sends = []
    def send(request, **kwargs):
        authorization = request.headers['Authorization']
        sends.append(authorization)
        if authorization.endswith('ou_actor_b'):
            from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
            return ToolHandlerOutcome('web_fetch', False, '合成认证或权限拒绝', error_code='PERMISSION_BLOCKED',
                result_envelope={'http_status': status})
        return RawResponseParts(200, {}, b'{}', request.url)
    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', send)
    failure = _fetch_with_reference(ctx, a)  # 模型即便直传A引用，宿主也必须用本人B，不能借共享引用绕优先级。
    assert not failure.ok
    after_failure, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    facts = after_failure.metadata['handler_details']['credential_discovery']
    assert facts['own_credential_present']
    assert facts['selected_credential_ref'] in {'', b} and facts['selected_credential_ref'] != a
    assert sends == ['Bearer SyntheticGroupSecret-ou_actor_b']
    revoked, _ = _credential_operation(ctx.agent, {'operation': 'revoke', 'credential_ref': b})
    assert revoked.ok, (revoked.error_code, revoked.output)
    after_revoke, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    facts = after_revoke.metadata['handler_details']['credential_discovery']
    assert not facts['own_credential_present'] and facts['selected_credential_ref'] == a
    assert _fetch_with_reference(ctx, a).ok
    assert sends == ['Bearer SyntheticGroupSecret-ou_actor_b', 'Bearer SyntheticGroupSecret-ou_actor_a']
    assert len(ctx.sent) == sent_before


# LLM: 查询与随后HTTP共用原当前run的网络上界；只做真实工具调用，不把预检测试的network-disabled边界借作后续网络授权。
# 函数用途: 在同一活动任务中执行发现或明确撤销，保持权限快照与实际HTTP一致。
def _credential_operation(agent, params):
    import uuid

    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    current = agent._current_run_params
    boundary = write_boundary_with_runtime_ledger(agent, current)
    result = execute_registry_test_call(agent.tools, 'request_credentials', params, write_boundary=boundary,
        run_id=current.run_id, call_id=uuid.uuid4().hex, register_with=agent)
    return result, boundary


@pytest.mark.parametrize('access_scope,expected', [(None, 'group'), ('group', 'group'), ('task', 'task')])
def test_explicit_request_scope_reaches_original_host_and_keeps_input_wait(tmp_path, access_scope, expected):
    from agent_py_agent.tests.test_group_shared_credentials import _group_case
    ctx = _group_case(tmp_path)
    params = {'operation': 'request', 'url': 'https://example.com/api'}
    if access_scope is not None:
        params['access_scope'] = access_scope
    result, _ = _credential_operation(ctx.agent, params)
    assert result.ok, (result.error_code, result.output)
    assert result.metadata['handler_details']['credential']['access_scope'] == expected
    assert result.metadata['handler_details']['input_dependency']['status'] == 'waiting_input'
    assert len(ctx.sent) == 1
    assert len(ctx.agent.conversation_store.tasks.load(ctx.agent._current_run_params.task_id).input_dependency_refs) == 1


@pytest.mark.parametrize('operation', ['request', 'find'])
def test_personal_source_cannot_select_or_discover_group_scope(tmp_path, operation):
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    agent = _agent(tmp_path, 'user')
    _active(agent)
    params = {'operation': operation, 'url': 'https://example.com/api'}
    if operation == 'request':
        params['access_scope'] = 'group'
    result, _ = _credential_operation(agent, params)
    assert not result.ok
    assert not agent.conversation_store.tasks.load(agent._current_run_params.task_id).input_dependency_refs


# LLM: 明确task范围来自原工具和签名卡，提交仍走原回调；不先伪造group字段来通过share验证。
# 函数用途: 建立真正旧群任务私密凭据，供显式转共享和跨owner拒绝测试。
def _old_task_credential(ctx):
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    result, _ = _credential_operation(ctx.agent, {'operation': 'request', 'access_scope': 'task',
        'authentication': 'bearer', 'url': 'https://example.com/api'})
    assert result.ok, (result.error_code, result.output)
    reference = result.metadata['handler_details']['credential']['credential_ref']
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='synthetic.old-task-card').payload
    assert row['access_scope'] == 'task'
    body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
    body.update(decision='submit', fields={'secret': 'SyntheticGroupSecret-ou_actor_a', 'credential_name': '旧任务站点'},
        submitter={'actor_id': 'ou_actor_a', 'chat_id': 'oc_synthetic', 'current_group_member': True})
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_actor_a')['ok']
    return reference


def test_real_old_task_share_then_new_member_uses_same_ciphertext_without_new_card(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    from agent_py_agent.tests.test_group_shared_credentials import _group_case, _switch_actor
    ctx = _group_case(tmp_path)
    reference = _old_task_credential(ctx)
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    before = read_json_object_report(path, context='synthetic').payload
    _switch_actor(ctx, 'ou_actor_b')
    upgrade, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    assert upgrade.ok
    facts = upgrade.metadata['handler_details']['credential_discovery']
    assert not facts['selected_credential_ref']
    assert reference in {item['credential_ref'] for item in facts['shareable_credentials']}
    _switch_actor(ctx, 'ou_actor_c')
    hidden, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    assert hidden.ok and reference not in hidden.output
    _switch_actor(ctx, 'ou_actor_b')
    shared, _ = _credential_operation(ctx.agent, {'operation': 'share', 'credential_ref': reference})
    assert shared.ok, (shared.error_code, shared.output)
    assert shared.metadata['handler_details']['credential']['access_scope'] == 'group'
    after = read_json_object_report(path, context='synthetic').payload
    assert after['ciphertext'] == before['ciphertext'] and after['credential_ref'] == reference
    assert after['reference'] != before['reference']
    _switch_actor(ctx, 'ou_actor_c')
    found, _ = _credential_operation(ctx.agent, {'operation': 'find', 'url': 'https://example.com/api'})
    assert found.ok and found.metadata['handler_details']['credential_discovery']['selected_credential_ref'] == reference
    requests = []
    def send(request, **kwargs):
        requests.append(request.headers['Authorization'])
        return RawResponseParts(200, {}, b'{}', request.url)
    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', send)
    assert _fetch_with_reference(ctx, reference).ok
    assert requests == ['Bearer SyntheticGroupSecret-ou_actor_a']
    assert len(ctx.sent) == 1
    assert not ctx.agent.conversation_store.tasks.load(ctx.agent._current_run_params.task_id).input_dependency_refs


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_other_owner_cannot_share_original_group_private_reference(tmp_path, kind):
    from dataclasses import replace

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.tests.test_group_shared_credentials import _group_case
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    ctx = _group_case(tmp_path / 'source')
    reference = _old_task_credential(ctx)
    if kind == 'user':
        other = _agent(tmp_path / 'other', 'user')
    else:
        other = SimpleAgent(replace(ctx.agent.config, my_agent_home=str(tmp_path / 'other-home'),
            my_agent_owner_id='oc_other'), tmp_path / 'other-workspace')
    _active(other)
    rejected, _ = _credential_operation(other, {'operation': 'share', 'credential_ref': reference})
    assert not rejected.ok
    assert not other.conversation_store.tasks.load(other._current_run_params.task_id).input_dependency_refs
    assert len(ctx.sent) == 1


def test_optional_shareable_metadata_is_preserved_without_implicit_mutation(discovery_tool):
    tool, calls, facts = discovery_tool
    facts.update(selected_credential_ref='', shareable_credentials=[{'credential_ref': 'cred_old_task', 'origin': 'https://example.com:443'}])
    result = tool.execute({'operation': 'find', 'url': 'https://example.com/api'})
    assert result.ok and json.loads(result.output)['shareable_credentials'] == facts['shareable_credentials']
    assert len(calls) == 1
    assert result.result_envelope.get('input_dependency') is None
