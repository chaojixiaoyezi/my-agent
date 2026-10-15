"""飞书本人 OAuth 使用真实 owner/TaskLink/输入/Wake 与执行器，外部登录和卡片运输都是合成接口。"""
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    input_dependency_path,
    task_has_waiting_input,
)
from agent_py_agent.agent.gateway_parts.feishu_oauth_service import oauth_callback_payload
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card

_FIXTURES = (background,)


# LLM: 共享真实临时 owner、claim 和签名库，配置只在宿主受控接缝注入；假适配器不会外发。
# 函数用途: 创建可向原管理员 actor 发私人登录卡的隔离环境。
@pytest.fixture
def ctx(background, monkeypatch):
    _ref, _path, _body, cards = _card(background)
    background.agent.config.feishu_app_id = 'synthetic-app'
    background.agent.config.feishu_app_secret = 'Synthetic-App-Secret'
    config = CapabilityConfig(feishu_oauth_redirect_uri='https://callback.example.invalid/feishu/oauth/callback')
    monkeypatch.setattr(oauth, 'deployment_capability_config', lambda home: config)
    background.oauth_config = config
    background.cards = cards
    background.api_calls = []
    return background


# LLM: 原函数创建输入和签名链接，测试只读本次假私人卡中结构化参数；不解析模型文案。
# 函数用途: 发起一次原授权并取得 callback 及准确输入引用。
def _authorize(ctx):
    dependency = oauth.request_oauth(ctx.agent)
    state = parse_qs(urlsplit(ctx.cards[-1]['elements'][1]['actions'][0]['url']).query)['state'][0]
    payload = oauth_callback_payload(ctx.agent, state)
    return dependency, state, payload


# LLM: 只替代官方接口响应，绑定 actor 与 token；生产记录、验证和加密流程不替换。
# 函数用途: 安装无网络 token/本人证明响应并记录调用次数。
def _api(ctx, monkeypatch, result=None, actor='ou_synthetic'):
    result = result or {'code': 0, 'access_token': 'Synthetic-Access-Token', 'expires_in': 3600,
                        'refresh_token': 'Synthetic-Refresh-Token', 'refresh_token_expires_in': 86400,
                        'scope': 'offline_access'}
    def send(method, path, body=None, token=''):
        ctx.api_calls.append((method, path))
        return {'code': 0, 'data': {'open_id': actor}} if path.endswith('/user_info') else result
    monkeypatch.setattr(oauth, '_oauth_api', send)


def _row(ctx):
    return read_json_object_report(oauth.oauth_path(ctx.agent, 'ou_synthetic'), context='test.oauth').payload


def test_real_executor_rejects_invocation_network_disabled_without_card(ctx):
    execute_approval_mode_operation(ctx.agent.home_paths, 'set', 'auto')
    count = len(ctx.cards)
    result = execute_registry_test_call(ctx.agent.tools, 'feishu_auth', {'action': 'authorize'},
        allowed_tools=['feishu_auth'], run_id=ctx.task_id, register_with=ctx.agent,
        write_boundary={'task_id': ctx.task_id, 'network_access_mode': 'disabled'})
    assert not result.ok and result.error_code == 'FEISHU_USER_AUTH_REQUIRED'
    assert len(ctx.cards) == count and not oauth.oauth_path(ctx.agent, 'ou_synthetic').exists()


def test_original_run_child_network_and_invalid_scope_reject_before_card(ctx):
    count = len(ctx.cards)
    ctx.agent._current_run_params.write_boundary = {'effective_permissions': {'network_enabled': False}}
    with pytest.raises(ValueError, match='网络'):
        oauth.request_oauth(ctx.agent)
    assert len(ctx.cards) == count
    ctx.agent._current_run_params.write_boundary = {}
    ctx.store.claims.finish({'thread_id': ctx.thread.thread_id, 'claim_id': ctx.claim['claim_id'], 'task_id': ctx.task_id, 'status': 'completed'})
    with pytest.raises(ValueError, match='执行'):
        oauth.request_oauth(ctx.agent)


def test_real_input_capacity_rejection_fails_oauth_without_card_and_allows_new_request(ctx):
    from agent_py_agent.agent.conversation.agent_tool_approval import (
        AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT,
    )
    from agent_py_agent.agent.conversation.input_dependencies import (
        create_input_dependency,
        resolve_input_dependency,
    )

    scope = oauth.tool_approval_scope(ctx.agent, ctx.task_id)
    link, error = ctx.store.tasks.load_report(ctx.task_id)
    assert not error and link is not None
    initial = len(link.input_dependency_refs)
    assert initial < AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT
    for index in range(AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT - initial):
        create_input_dependency(ctx.agent, scope, input_id=f'synthetic-capacity-{index}',
            provider='synthetic', expires_at=time.time() + 1800)
    count = len(ctx.cards)
    with pytest.raises(ValueError, match='本人授权卡未送达或输入登记失败'):
        oauth.request_oauth(ctx.agent)
    failed = _row(ctx)
    assert failed['status'] == 'failed' and not failed['ciphertext']
    assert len(ctx.cards) == count and not input_dependency_path(ctx.agent, failed['input_id']).exists()
    link, error = ctx.store.tasks.load_report(ctx.task_id)
    assert not error and len(link.input_dependency_refs) == AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT
    arm_input_dependency(ctx.agent, 'synthetic-capacity-0')
    resolve_input_dependency(ctx.agent, 'synthetic-capacity-0', status='cancelled')
    result = oauth.request_oauth(ctx.agent)
    assert result['status'] == 'waiting_input' and result['input_id'] != failed['input_id']
    assert _row(ctx)['status'] == 'pending' and len(ctx.cards) == count + 1


def test_ready_payload_secret_free_and_original_wake_replayed_once(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    arm_input_dependency(ctx.agent, dependency['input_id'])
    _api(ctx, monkeypatch)
    result = oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert result == {'ok': True, 'status': 'ready'} and len(ctx.api_calls) == 2
    oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert len(ctx.api_calls) == 2 and len(ctx.store.wakes.pending(limit=0)) == 1
    assert not task_has_waiting_input(ctx.agent, ctx.task_id)
    row = _row(ctx)
    assert row['status'] == 'ready' and 'Synthetic-Access-Token' not in json.dumps(row)
    assert 'Synthetic-Refresh-Token' not in json.dumps(row)
    public = json.dumps(oauth.oauth_status(ctx.agent))
    assert 'token' not in public and state not in public and 'callback.example' not in public
    assert oauth.user_access_token(ctx.agent) == 'Synthetic-Access-Token'


def test_ready_commit_before_wake_failure_repeated_callback_recovers_without_exchange(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    arm_input_dependency(ctx.agent, dependency['input_id'])
    _api(ctx, monkeypatch)
    resolve = oauth.resolve_input_dependency
    monkeypatch.setattr(oauth, 'resolve_input_dependency', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('synthetic')))
    with pytest.raises(ValueError, match='恢复提交'):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'ready'
    monkeypatch.setattr(oauth, 'resolve_input_dependency', resolve)
    assert oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')['ok']
    assert len(ctx.api_calls) == 2 and len(ctx.store.wakes.pending()) == 1


@pytest.mark.parametrize('failure', ['exchange', 'wrong_actor', 'transport'])
def test_failure_is_terminal_and_cannot_reuse_authorization_code(ctx, monkeypatch, failure):
    if failure == 'transport':
        monkeypatch.setattr(ctx.agent.delivery_service, 'deliver_control_card', lambda *args, **kwargs: SimpleNamespace(ok=False))
        with pytest.raises(ValueError):
            oauth.request_oauth(ctx.agent)
        row = _row(ctx)
        dependency = read_json_object_report(input_dependency_path(ctx.agent, row['input_id']), context='test').payload
        assert row['status'] == 'failed' and dependency['status'] == 'failed'
        return
    dependency, state, payload = _authorize(ctx)
    arm_input_dependency(ctx.agent, dependency['input_id'])
    if failure == 'exchange':
        monkeypatch.setattr(oauth, '_oauth_api', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('Synthetic-Secret-Error')))
    else:
        _api(ctx, monkeypatch, actor='ou_other')
    with pytest.raises(ValueError) as caught:
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert 'Synthetic-Secret' not in str(caught.value)
    row = _row(ctx)
    assert row['status'] == 'failed' and not row['ciphertext']
    assert read_json_object_report(input_dependency_path(ctx.agent, dependency['input_id']), context='test').payload['status'] == 'failed'
    assert len(ctx.store.wakes.pending()) == 1
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')


@pytest.mark.parametrize('field,value', [('feishu_oauth_redirect_uri', 'https://changed.invalid/feishu/oauth/callback'),
    ('feishu_oauth_scopes', ['offline_access', 'changed:scope']), ('feishu_app_id', 'other-app'), ('feishu_app_secret', 'Other-App-Secret')])
def test_configuration_change_rejects_original_callback(ctx, monkeypatch, field, value):
    dependency, state, payload = _authorize(ctx)
    arm_input_dependency(ctx.agent, dependency['input_id'])
    _api(ctx, monkeypatch)
    target = ctx.agent.config if field.startswith('feishu_app') else ctx.oauth_config
    setattr(target, field, value)
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert ctx.api_calls == [] and _row(ctx)['status'] == 'failed'


def test_local_admin_unbound_or_other_owner_cannot_complete(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch)
    remove_admin_channel_identity(ctx.agent.home_paths.root, 'feishu', 'ou_synthetic')
    with pytest.raises(ValueError, match='身份'):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert ctx.api_calls == []
    with pytest.raises(ValueError, match='owner'):
        oauth.complete_oauth(ctx.agent, {**payload, 'owner': {'provider': 'feishu', 'owner_kind': 'user', 'owner_id': 'other'}}, state, 'Synthetic-Code')


@pytest.mark.parametrize('field,value', [('expires_in', None), ('expires_in', True), ('expires_in', '3600'),
    ('expires_in', float('nan')), ('expires_in', float('inf')), ('expires_in', -1),
    ('refresh_token_expires_in', None), ('refresh_token_expires_in', True), ('refresh_token_expires_in', '86400')])
def test_token_lifetime_is_not_guessed_from_missing_or_invalid_fields(ctx, monkeypatch, field, value):
    dependency, state, payload = _authorize(ctx)
    response = {'code': 0, 'access_token': 'Synthetic-Access-Token', 'expires_in': 3600,
                'refresh_token': 'Synthetic-Refresh-Token', 'refresh_token_expires_in': 86400}
    if value is None:
        response.pop(field)
    else:
        response[field] = value
    _api(ctx, monkeypatch, response)
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'failed'


def test_explicit_access_only_response_and_missing_granted_scope_remain_truthful(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch, {'code': 0, 'access_token': 'Synthetic-Access-Token', 'expires_in': 3600})
    assert oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')['ok']
    row = _row(ctx)
    assert row['granted_scopes'] == ''
    value = json.loads(oauth._cipher(ctx.agent.home_paths.owner_home_dir).decrypt(row['ciphertext'].encode()))
    assert value['refresh_token'] == '' and row['refresh_expires_at'] < row['ready_expires_at']


def test_expired_status_revoke_and_network_revocation_prevent_token_use(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch)
    oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    ctx.agent._current_run_params.write_boundary = {'network_access_mode': 'disabled'}
    with pytest.raises(ValueError, match='网络'):
        oauth.user_access_token(ctx.agent)
    ctx.agent._current_run_params.write_boundary = {}
    path = oauth.oauth_path(ctx.agent, 'ou_synthetic')
    row = _row(ctx)
    path.write_text(json.dumps({**row, 'ready_expires_at': time.time() - 1}))
    assert oauth.oauth_status(ctx.agent)['status'] == 'expired'
    assert oauth.oauth_status(ctx.agent, revoke=True)['status'] == 'revoked'
    with pytest.raises(ValueError):
        oauth.user_access_token(ctx.agent)


def test_refresh_does_not_hold_record_lock_and_revocation_wins(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch)
    oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    row = _row(ctx)
    oauth.oauth_path(ctx.agent, 'ou_synthetic').write_text(json.dumps({**row, 'ready_expires_at': time.time() - 1}))
    def revoke_during_network(method, path, body=None, token=''):
        if path.endswith('/oauth/token'):
            assert oauth.oauth_status(ctx.agent, revoke=True)['status'] == 'revoked'
            return {'code': 0, 'access_token': 'Refreshed-Token', 'expires_in': 3600}
        return {'code': 0, 'data': {'open_id': 'ou_synthetic'}}
    monkeypatch.setattr(oauth, '_oauth_api', revoke_during_network)
    with pytest.raises(ValueError, match='变化'):
        oauth.user_access_token(ctx.agent)
    assert _row(ctx)['status'] == 'revoked'


@pytest.mark.parametrize('data', [[], ['wrong'], 'wrong', 1, None])
def test_actor_proof_wrong_shape_fails_without_attribute_error(ctx, monkeypatch, data):
    dependency, state, payload = _authorize(ctx)
    def malformed(method, path, body=None, token=''):
        return {'code': 0, 'data': data} if path.endswith('/user_info') else {'access_token': 'Synthetic-Token', 'expires_in': 3600}
    monkeypatch.setattr(oauth, '_oauth_api', malformed)
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'failed'


@pytest.mark.parametrize('refresh', [None, True, {}, [], 123])
def test_refresh_token_must_be_string(ctx, monkeypatch, refresh):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch, {'access_token': 'Synthetic-Token', 'expires_in': 3600, 'refresh_token': refresh})
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'failed'


def test_network_token_callback_change_after_authorize_is_terminal(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch)
    ctx.agent._current_run_params.write_boundary = {'network_access_mode': 'disabled'}
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert ctx.api_calls == [] and _row(ctx)['status'] == 'failed'


def test_exchange_does_not_hold_record_lock_and_revoke_is_not_overwritten(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    def revoke_during_network(method, path, body=None, token=''):
        if path.endswith('/oauth/token'):
            assert oauth.oauth_status(ctx.agent, revoke=True)['status'] == 'revoked'
            return {'access_token': 'Synthetic-Token', 'expires_in': 3600}
        return {'data': {'open_id': 'ou_synthetic'}}
    monkeypatch.setattr(oauth, '_oauth_api', revoke_during_network)
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'revoked'


def test_missing_app_and_bad_deployment_are_not_claimed_ready(ctx):
    tool = ctx.agent.tools.tools['feishu_auth']
    assert tool.availability().available
    ctx.agent.config.feishu_app_secret = ''
    assert not tool.availability().available
    ctx.agent.config.feishu_app_secret = 'Synthetic-App-Secret'
    ctx.oauth_config.feishu_oauth_redirect_uri = ''
    assert not tool.availability().available


def test_original_owner_provider_is_authority_not_visible_role(ctx):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.feishu_auth_tool import FeishuAuthTool
    for kind in ['user', 'group']:
        agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(ctx.agent.home_paths.root),
            my_agent_owner_provider='feishu', my_agent_owner_kind=kind, my_agent_owner_id='ou_ordinary'),
            ctx.agent.root / kind)
        agent.config.feishu_app_id = 'synthetic-app'
        agent.config.feishu_app_secret = 'Synthetic-App-Secret'
        if kind == 'group':
            assert not FeishuAuthTool(agent).availability().available
            with pytest.raises(ValueError):
                oauth.oauth_actor(agent)
        else:
            assert oauth.oauth_actor(agent) == 'ou_ordinary'
            with pytest.raises(ValueError):
                oauth._assert_actor(agent, 'ou_synthetic')
        with pytest.raises(ValueError):
            oauth.complete_oauth(agent, {'owner': {'provider': 'local', 'owner_kind': 'main', 'owner_id': 'local/main'},
                'actor_id': 'ou_synthetic', 'input_id': 'not-owned'}, 'not-owned-state', 'code')


def test_current_child_network_permission_is_checked_again(ctx):
    child = ctx.agent.subagents.create_run(goal='OAuth scope', thought='read', plan=['read'],
        parent_id=ctx.task_id, root_id=ctx.task_id, allowed_tools=['feishu_auth'])
    ctx.agent.subagents.mutate(child.id, lambda task: setattr(task, 'effective_permissions', {'network_enabled': False}))
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    with pytest.raises(ValueError, match='子代理网络'):
        oauth._assert_network(ctx.agent, tool_approval_scope(ctx.agent, child.id))


def test_status_config_change_does_not_report_usable_ready(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch)
    oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    ctx.oauth_config.feishu_oauth_scopes = ['offline_access', 'new:scope']
    assert oauth.oauth_status(ctx.agent)['status'] == 'configuration_changed'
    with pytest.raises(ValueError):
        oauth.user_access_token(ctx.agent)


def test_public_callback_rejects_tampered_state_and_exact_owner_replay(ctx):
    dependency, state, payload = _authorize(ctx)
    forged = state[:-1] + ('0' if state[-1] != '0' else '1')
    with pytest.raises(ValueError):
        oauth_callback_payload(ctx.agent, forged)
    assert payload['owner']['owner_kind'] == 'main'


def test_only_shared_deployment_oauth_config_is_used(ctx, monkeypatch):
    from agent_py_agent.agent.capability.runtime_config_reload import deployment_capability_config
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    base = ctx.agent.home_paths.root
    (base / 'config').mkdir(exist_ok=True)
    (base / 'config/capability_config.yaml').write_text('feishu_oauth_redirect_uri: https://shared.invalid/feishu/oauth/callback\nfeishu_oauth_scopes:\n  - offline_access\n')
    ordinary = SimpleAgent(AgentConfig(my_agent_home=str(base), my_agent_owner_provider='feishu',
        my_agent_owner_kind='user', my_agent_owner_id='ou_ordinary'), ctx.agent.root / 'ordinary-config')
    local = ordinary.home_paths.owner_home_dir / 'config'
    local.mkdir(exist_ok=True)
    (local / 'capability_config.yaml').write_text('feishu_oauth_redirect_uri: https://other.invalid/feishu/oauth/callback\nfeishu_oauth_scopes:\n  - other:scope\n')
    monkeypatch.setattr(oauth, 'deployment_capability_config', deployment_capability_config)
    assert oauth.oauth_configuration(ordinary) == ('https://shared.invalid/feishu/oauth/callback', ['offline_access'])


@pytest.mark.parametrize('operation', [oauth.request_oauth, oauth.user_access_token])
def test_missing_execution_context_is_readable_rejection_without_card_or_network(ctx, operation):
    original_card_count = len(ctx.cards)
    ctx.agent._current_run_params = None
    with pytest.raises(ValueError, match='执行上下文'):
        operation(ctx.agent)
    assert len(ctx.cards) == original_card_count and ctx.api_calls == []


def test_real_tool_fake_llm_uses_input_archive_boundary_once(ctx):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.test_task_progress_advisory import _CaptureBackend
    execute_approval_mode_operation(ctx.agent.home_paths, 'set', 'auto')
    class Backend(_CaptureBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(prompt)
            assert len(self.prompts) <= 2
            if len(self.prompts) == 2:
                assert not kwargs.get('tools') and '[natural-user-reply]' in prompt
                return ModelResponse(text='请完成已发出的本人授权，之后继续原工作。', backend=self.name)
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                'id': 'oauth-private-input', 'name': 'feishu_auth', 'input': {'action': 'authorize'}}])
    backend = Backend()
    ctx.agent.backend = backend
    result = ctx.agent.run('读取本人已授权的云文档，缺本人授权时向我申请。', params=RunParams(
        source='background_main_agent', save=False, run_id=ctx.task_id, task_id=ctx.task_id, allowed_tools=['feishu_auth'],
        task_attributes={'conversation_thread_id': ctx.thread.thread_id, 'conversation_task_id': ctx.task_id}))
    assert result.runtime_status == 'needs_user_input'
    assert result.runtime_reason == 'TASK_INPUT_WAIT' and result.turn_end_reason == 'blocked'
    assert len(result.archive_tool_calls) == 1 and len(backend.prompts) == 2
    facts = result.archive_tool_calls[0]['tool_result_envelope']['input_dependency']
    assert facts['status'] == 'waiting_input'
    archived = json.dumps(result.archive_tool_calls)
    assert 'Synthetic-App-Secret' not in archived and 'accounts.feishu.cn' not in archived and 'code_verifier' not in archived


def test_corrupt_pkce_is_failed_and_corrupt_ready_cipher_is_not_exposed(ctx, monkeypatch):
    dependency, state, payload = _authorize(ctx)
    path = oauth.oauth_path(ctx.agent, 'ou_synthetic')
    row = _row(ctx)
    path.write_text(json.dumps({**row, 'ciphertext': 'Synthetic-Bad-Cipher'}))
    _api(ctx, monkeypatch)
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'failed' and ctx.api_calls == []
    dependency, state, payload = _authorize(ctx)
    oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    path.write_text(json.dumps({**_row(ctx), 'ciphertext': 'Synthetic-Bad-Cipher'}))
    with pytest.raises(ValueError) as caught:
        oauth.user_access_token(ctx.agent)
    assert 'Synthetic-Bad-Cipher' not in str(caught.value)


@pytest.mark.parametrize('response,ok', [
    ({'access_token': 'Synthetic-Token', 'expires_in': 3600}, True),
    ({'code': 0, 'access_token': 'Synthetic-Token', 'expires_in': 3600}, True),
    ({'code': True, 'access_token': 'Synthetic-Token'}, False),
    ({'code': 1, 'access_token': 'Synthetic-Token'}, False),
    ({'code': 0}, False),
    ({'error': 'invalid_grant', 'access_token': 'Synthetic-Token'}, False),
])
def test_real_oauth_http_adapter_accepts_documented_code_omission_only(response, ok, monkeypatch):
    from contextlib import nullcontext
    encoded = json.dumps(response).encode()
    monkeypatch.setattr(oauth, 'open_authenticated_request', lambda *args, **kwargs: nullcontext(
        SimpleNamespace(read=lambda limit: encoded)))
    if ok:
        assert oauth._oauth_api('POST', '/authen/v2/oauth/token', {'code': 'Synthetic-Code'}) == response
    else:
        with pytest.raises(ValueError):
            oauth._oauth_api('POST', '/authen/v2/oauth/token', {'code': 'Synthetic-Code'})


def test_user_info_still_requires_api_status_and_real_open_id(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(oauth, 'open_authenticated_request', lambda *args, **kwargs: nullcontext(
        SimpleNamespace(read=lambda limit: b'{"data":{"open_id":"ou_synthetic"}}')))
    with pytest.raises(ValueError):
        oauth._verify_actor('Synthetic-Token', 'ou_synthetic')


@pytest.mark.parametrize('token_type', ['DPoP', '', None, True])
def test_unsupported_token_type_is_not_used_as_bearer(ctx, monkeypatch, token_type):
    dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch, {'access_token': 'Synthetic-Token', 'expires_in': 3600, 'token_type': token_type})
    with pytest.raises(ValueError):
        oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')
    assert _row(ctx)['status'] == 'failed'
