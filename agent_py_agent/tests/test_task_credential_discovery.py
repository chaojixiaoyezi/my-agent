"""凭据发现只返回原宿主裁决；原工具schema/Registry与合成owner，不读取真实凭据。"""
# LLM: Exercise authorized discovery and real cold-card adapters with synthetic owners; keep the parameter schema unchanged.
# 模块用途: 验证凭据发现、真实安全卡入口及精简描述，不读取或发送真实秘密。
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
        # 换 home 时不能沿用源 Agent 构造期冻结的 host_home_snapshot（640e478ee），交由新构造重新解析。
        other = SimpleAgent(replace(ctx.agent.config, my_agent_home=str(tmp_path / 'other-home'),
            my_agent_owner_id='oc_other', host_home_snapshot=None), tmp_path / 'other-workspace')
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


@pytest.mark.parametrize('reference', ['cred_synthetic', '', None])
def test_find_reference_conflict_is_rejected_before_host_discovery(discovery_tool, reference):
    tool, calls, _ = discovery_tool
    params = {'operation': 'find', 'url': 'https://example.com/api', 'credential_ref': reference}
    assert not validate_tool_input(params, tool.model_spec.input_schema).ok
    result = tool.execute(params)
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert result.effect_outcome == 'not_started'
    assert calls == []
    assert 'status' in result.output and 'credential_ref' in result.output


# LLM: Candidate wording changes, but reference status must stay distinct from same-origin discovery.
# 函数用途: 核对精简文案仍明确已有凭据引用的状态查询与空匹配边界。
def test_tool_catalog_distinguishes_reference_status_from_origin_discovery():
    spec = RequestCredentialsTool(SimpleNamespace()).model_spec
    assert '已有credential_ref用status查安全状态' in spec.description
    assert '空匹配不证明旧引用未填写' in spec.description
    assert '占位' in spec.input_schema['properties']['url']['description']
    assert '不透明' in spec.input_schema['properties']['credential_ref']['description']
    assert any('status' in hint for hint in spec.hints.use_cases)


@pytest.mark.parametrize('kind', ['group', 'user'])
def test_saved_reference_status_remains_authorized_metadata_not_discovery(tmp_path, kind, monkeypatch):
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    from agent_py_agent.tests.test_ingestion_task_credential_scope import (
        _SECRET,
        _active,
        _agent,
        _credential,
    )
    agent = _agent(tmp_path, kind)
    _active(agent)
    reference = _credential(agent, 'https://example.com/api')
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    before = path.read_bytes()
    monkeypatch.setattr('agent_py_agent.agent.user_space.task_credential_groups.discover_group_credentials',
        lambda *_a, **_kw: {'credentials': [], 'selected_credential_ref': ''})
    empty, _ = _credential_operation(agent, {'operation': 'find', 'url': 'https://other.example/api'})
    assert empty.ok and json.loads(empty.output)['credentials'] == []
    result, _ = _credential_operation(agent, {'operation': 'status', 'credential_ref': reference})
    assert result.ok and result.handler_executed
    payload = json.loads(result.output)
    assert payload['credential_ref'] == reference and payload['status'] == 'ready'
    assert payload['origin'] == 'https://example.com:443'
    assert set(payload) == {'credential_ref', 'status', 'origin', 'expires_at', 'authentication', 'authentication_configured', 'verification_status', 'replaces_ref'}
    assert payload['authentication'] == 'bearer' and payload['authentication_configured'] is True
    assert _SECRET not in result.output and 'ciphertext' not in result.output
    assert path.read_bytes() == before


# LLM: 合成冷群会话只启原词法工具检索，禁止可选embedding外发；原注册/活动任务/权限照常装配，不接真实通道。
# 函数用途: 为安全发卡的名卡、搜索与出站Schema回归准备普通临时Agent。
def _cold_card_agent(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active
    agent = SimpleAgent(AgentConfig(model_backend='echo',enable_plugins=False,tool_vector_search_enabled=False,
        my_agent_home=str(tmp_path/'home'),my_agent_owner_provider='feishu',my_agent_owner_kind='group',
        my_agent_owner_id='oc_synthetic'),tmp_path/'workspace')
    _active(agent)
    return agent


# LLM: Align only literal prose with the reviewed candidate; keep request, safety, URL and schema assertions.
# 函数用途: 验证精简名卡仍优先说明真实安全发卡、确认状态和无需查模板。
def test_cold_card_metadata_explains_real_collection_before_saved_reference_lookup():
    spec = RequestCredentialsTool(SimpleNamespace()).model_spec
    lead = spec.description.split('已有credential_ref',1)[0]
    assert 'request常规账号密码用collection_mode=username_password' in lead and '用真实安全卡收集秘密' in lead
    assert 'authentication=collected时url可由用户在卡片填' in lead and '发卡不访问目标' in lead
    assert 'request_delivery.status=confirmed' in lead and '禁止要求在普通聊天填写' in lead
    assert 'waiting_input不等于已填或已保存' in lead
    assert any('无须先找模板或历史' in case for case in spec.hints.use_cases)
    assert any('安全输入卡' in case and 'request' in case for case in spec.hints.use_cases)
    assert {'账号密码','输入卡','安全表单'} <= set(spec.hints.keywords)
    assert any('聊天表格' in note for note in spec.hints.avoid_when)
    assert validate_tool_input({'operation':'request','collection_mode':'username_password'},spec.input_schema).ok
    assert not validate_tool_input({'operation':'find','url':'https://example.com','credential_ref':'cred_synthetic'},spec.input_schema).ok


# LLM: Real cold search still returns exactly the currently authorized schema and candidate description.
# 函数用途: 保留自然语言发卡检索与授权门回归，只同步候选措辞。
@pytest.mark.parametrize('query',['新的账号密码输入卡','安全表单收集认证信息','申请凭据卡'])
def test_cold_card_natural_search_recovers_only_current_authorized_schema(tmp_path,query):
    from dataclasses import replace

    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    agent = _cold_card_agent(tmp_path)
    snapshot = replace(agent.tools.runtime_snapshot(),presentation_deferred_names=frozenset({'request_credentials'}))
    assert 'request_credentials' not in {s.name for s in agent.tools.model_visible_specs(runtime_snapshot=snapshot)}
    result = agent.tools.tools['tool_search'].execute_scoped({'query':query},ToolInvocationContext(snapshot))
    tools = json.loads(result.output)['tools']
    found = next(tool for tool in tools if tool['name']=='request_credentials')
    assert found['input_schema'] == agent.tools.tools['request_credentials'].model_spec.input_schema
    assert 'request常规账号密码' in found['description'] and '真实安全卡' in found['description']
    loaded = set(result.result_envelope['tool_search']['loaded_tool_names'])
    assert 'request_credentials' in loaded
    assert 'request_credentials' in {s.name for s in agent.tools.model_visible_specs(runtime_snapshot=snapshot,loaded_tool_names=loaded)}


# LLM: Both real payload projectors must retain identical schemas and the no-network/confirmed contract.
# 函数用途: 核对候选描述经两个供应商入口原样出站，参数不变。
@pytest.mark.parametrize('provider',['anthropic','chat'])
def test_cold_card_metadata_reaches_original_provider_payload_without_schema_changes(provider):
    from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
    spec = RequestCredentialsTool(SimpleNamespace()).model_spec
    backend_type = AnthropicCompatibleBackend if provider=='anthropic' else OpenAICompatibleBackend
    backend = backend_type(BackendOptions(api_base='https://example.com',api_key='synthetic',model_name='synthetic',stream_enabled=False))
    payload = backend.project_generate_payload('申请一个安全输入界面，不访问网站。',tools=[{
        'name':spec.name,'description':spec.description,'input_schema':spec.input_schema}])
    tool = payload['tools'][0] if provider=='anthropic' else payload['tools'][0]['function']
    assert tool['name']==spec.name and tool['description']==spec.description
    assert tool['input_schema' if provider=='anthropic' else 'parameters']==spec.input_schema
    assert 'request_delivery.status=confirmed' in tool['description'] and '发卡不访问目标' in tool['description']
    assert not spec.hints.default_deferred


def test_cold_card_search_cannot_restore_hidden_tool_authority(tmp_path):
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    agent = _cold_card_agent(tmp_path)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=['tool_search'])
    result = agent.tools.tools['tool_search'].execute_scoped({'query':'账号密码输入卡'},ToolInvocationContext(snapshot))
    assert result.ok and json.loads(result.output)['tools']==[]
    assert result.result_envelope['tool_search']['loaded_tool_names']==[]


def test_cold_card_real_registry_request_without_url_sends_only_host_form(tmp_path):
    from agent_py_agent.agent.delivery import ChannelCapabilities
    from agent_py_agent.tests.test_im_credential_card_preflight import _invoke
    agent = _cold_card_agent(tmp_path)
    sent = []
    agent.channel_registry.register_adapter('feishu',SimpleNamespace(send_control_card=lambda target,card,**kwargs:
        sent.append((target,card)) or {'message_id':'om_synthetic','chat_id':'oc_synthetic'}),
        capabilities=ChannelCapabilities(control_cards=True,proactive=True))
    result,boundary = _invoke(agent,{'operation':'request','collection_mode':'username_password'})
    assert result.ok and result.handler_executed and len(sent)==1
    assert boundary['network_access_mode']=='disabled'
    facts = result.metadata['handler_details']['credential']
    assert facts['status']=='waiting_input' and facts['request_delivery']['status']=='confirmed'
    assert facts['origin']==''
    assert {'target_url','username','secret'} <= {item.get('name') for item in sent[0][1]['elements'][1]['elements']}
