"""模型卡经原 /model 与 private-interaction 入口的身份、版本及秘密回归。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import (
    model_control_cards,
    model_profile_service,
    private_interaction_service,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_model_profiles import Host, add


# LLM: 测试 transport 只登记真实形状卡和回执，不实现业务或替被测对象写配置。
# 类用途: 隔离飞书外发，让测试从原控制入口检查发送目标与卡片内容。
class CardDelivery:
    # LLM: 测试投递账本仅用于断言，不决定 owner 权限。
    # 函数用途: 初始化隔离的发送记录。
    def __init__(self):
        self.sent = []

    # LLM: 回执字段严格来自 transport 替身，业务代码须自行保存和核验。
    # 函数用途: 捕获卡片、原目标并返回稳定消息编号。
    def deliver_control_card(self, context, card, *, summary):
        message_id = f'om_{len(self.sent)}'
        chat_id = context.target if context.target.startswith('oc_') else 'oc_private'
        self.sent.append((context, card, message_id, chat_id))
        return SimpleNamespace(ok=True, message_id=message_id, chat_id=chat_id)


# LLM: 冻结测试 owner 与原 actor；只是替换信任边界身份来源，不替卡片保存业务结果。
# 函数用途: 组装个人或群控制场景。
def setup(tmp_path, monkeypatch, *, group=False, admin=False):
    base = Host(tmp_path)
    base.home_paths = home_paths(tmp_path)
    base.config.feishu_session_lock_enabled = False
    base.delivery_service = CardDelivery()
    owner = OwnerIdentity.local_main() if admin else (OwnerIdentity.provider_group('feishu', 'oc_group') if group else OwnerIdentity.provider_user('feishu', 'ou_actor'))
    monkeypatch.setattr(model_profile_service, 'resolve_gateway_scope_owner', lambda *_: owner)
    monkeypatch.setattr('agent_py_agent.agent.capability.runtime_config_reload.capability_config_for_agent',
                        lambda *_: SimpleNamespace(im_model_cards_enabled=True))
    if admin:
        monkeypatch.setattr(private_interaction_service, 'admin_channel_identity_for_request', lambda *_: object())
    scope = GatewayControlScope(user_id='ou_actor', channel='feishu', conversation_id='oc_group' if group else 'oc_private',
                                metadata={'channel_chat_type': 'group' if group else 'p2p'})
    host, thread = model_profile_service._scoped_model_host(base, scope)
    if admin:
        base.conversation_store = host.conversation_store
    return base, owner, scope, host, thread


# LLM: 卡引用从被测对象发出的按钮取得，保持外部 SDK 回调原形状。
# 函数用途: 构造一张已发送卡的私人控制请求。
def callback(base, *, index=-1, decision='add', fields=None):
    _, card, message_id, chat_id = base.delivery_service.sent[index]
    raw = json.dumps(card)
    import re
    reference = re.search(r'"reference": "([^"]+)"', raw).group(1)
    return {'reference': reference, 'decision': decision, 'message_id': message_id, 'chat_id': chat_id,
            'fields': fields if fields is not None else {'profile_id': ''}}


# LLM: 回调不直接修改目录，完整走生产验签、密码和原卡复核。
# 函数用途: 用已认证测试 actor 提交模型卡控制。
def submit(base, body, actor='ou_actor'):
    return private_interaction_service.resolve_private_interaction(base, body, channel='feishu', actor_id=actor)


@pytest.mark.parametrize('group', [False, True])
def test_original_route_private_form_preserves_original_owner_thread(tmp_path, monkeypatch, group):
    base, owner, scope, host, thread = setup(tmp_path, monkeypatch, group=group)
    result = model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    assert result.ok
    assert base.delivery_service.sent[0][0].target == ('oc_group' if group else 'ou_actor')
    assert submit(base, callback(base))['ok']
    assert base.delivery_service.sent[-1][0].target == 'ou_actor'
    form = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
            'model_name': 'private-model', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-private-key'}
    reply = submit(base, callback(base, decision='save', fields=form))
    assert reply['ok']
    catalog = read_model_profiles(model_profiles_path(host.home_paths))
    profile_id = next(iter(catalog['profiles']))
    assert catalog['providers'][catalog['profiles'][profile_id]['provider_id']]['api_key'] == form['api_key']
    assert host.conversation_store.threads.load(thread.thread_id).model_profile_id != profile_id
    assert form['api_key'] not in json.dumps(base.delivery_service.sent, default=str)
    for path in host.home_paths.owner_home_dir.rglob('model-*.json'):
        assert form['api_key'] not in path.read_text()
    # 私人回调不能给 actor 的个人目录增加群目录之外的配置。
    if group:
        personal = model_control_cards.model_control_host(base, OwnerIdentity.provider_user('feishu', 'ou_actor'))
        assert not read_model_profiles(model_profiles_path(personal.home_paths))['profiles']


@pytest.mark.parametrize('change', [{'chat_id': 'oc_forward'}, {'message_id': 'om_forward'}])
def test_forward_wrong_chat_rejected_before_form(tmp_path, monkeypatch, change):
    base, _, scope, _, _ = setup(tmp_path, monkeypatch, group=True)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base)
    with pytest.raises(ValueError):
        submit(base, {**body, **change})
    with pytest.raises(ValueError):
        submit(base, body, actor='ou_other')
    assert len(base.delivery_service.sent) == 1


def test_unlock_and_deployment_off_gate(tmp_path, monkeypatch):
    base, _, scope, _, _ = setup(tmp_path, monkeypatch)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base)
    base.config.feishu_session_lock_enabled = True
    assert submit(base, body)['error_code'] == 'IM_PASSWORD_SETUP_REQUIRED'
    monkeypatch.setattr(private_interaction_service, 'private_interaction_lock_error', lambda *_: 'IM_UNLOCK_REQUIRED')
    assert submit(base, body)['error_code'] == 'IM_UNLOCK_REQUIRED'
    monkeypatch.setattr(private_interaction_service, 'private_interaction_lock_error', lambda *_: '')
    monkeypatch.setattr('agent_py_agent.agent.capability.runtime_config_reload.capability_config_for_agent',
                        lambda *_: SimpleNamespace(im_model_cards_enabled=False))
    with pytest.raises(ValueError):
        submit(base, body)


@pytest.mark.parametrize('admin', [False, True])
def test_catalog_and_selection_revision_reject_stale_cards(tmp_path, monkeypatch, admin):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch, admin=admin)
    profile_id, _ = add(host)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base, decision='select', fields={'profile_id': profile_id})
    execute_model_profile_operation(host, 'select', {'profile_id': profile_id}, thread_id=thread.thread_id)
    with pytest.raises(ValueError):
        submit(base, body)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base, decision='set_default', fields={'profile_id': profile_id})
    add(host, model_name='second')
    with pytest.raises(ValueError):
        submit(base, body)


def test_edit_keeps_id_blank_key_and_requires_new_key_on_endpoint_change(tmp_path, monkeypatch):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch)
    profile_id, _ = add(host)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    submit(base, callback(base, decision='edit', fields={'profile_id': profile_id}))
    form = {'model_backend': 'anthropic_compatible', 'api_base': 'https://changed.test',
            'model_name': 'edited', 'model_context_window_tokens': '96000', 'api_key': ''}
    body = callback(base, decision='save', fields=form)
    with pytest.raises(ValueError):
        submit(base, body)
    form['api_base'] = 'https://example.test/anthropic'
    assert submit(base, callback(base, decision='save', fields=form))['ok']
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert list(data['profiles']) == [profile_id]
    assert data['profiles'][profile_id]['model_name'] == 'edited'
    assert len(data['providers']) == 1
    assert data['providers'][data['profiles'][profile_id]['provider_id']]['api_key'] == 'only-private-secret'
    assert submit(base, callback(base, decision='save', fields=form))['ok']
    assert read_model_profiles(model_profiles_path(host.home_paths)) == data


def test_no_open_card_network_or_agent_initialization(tmp_path, monkeypatch):
    base, _, scope, _, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(private_interaction_service, '_resolve_request_agent_for_owner', lambda *_: pytest.fail('cold Agent initialized'))
    result = model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    assert result.ok and submit(base, callback(base))['ok']


def test_shared_choices_cannot_edit_or_expose_administrator_credentials(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.shared_model_catalog import set_shared_profile
    base, _, scope, host, thread = setup(tmp_path, monkeypatch, group=True)
    admin = model_control_cards.model_control_host(base, OwnerIdentity.local_main())
    profile_id, _ = add(admin, api_key='admin-private-credential')
    set_shared_profile(admin, profile_id, True)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    assert 'admin-private-credential' not in json.dumps(base.delivery_service.sent, default=str)
    assert 'example.test' not in json.dumps(base.delivery_service.sent, default=str)
    body = callback(base, decision='edit', fields={'profile_id': 'shared:' + profile_id})
    with pytest.raises(ValueError):
        submit(base, body)
    assert submit(base, callback(base, decision='select', fields={'profile_id': 'shared:' + profile_id}))['ok']
    assert host.conversation_store.threads.load(thread.thread_id).model_profile_id == 'shared:' + profile_id
    count = len(base.delivery_service.sent)
    assert submit(base, callback(base, decision='select', fields={'profile_id': 'shared:' + profile_id}))['ok']
    assert len(base.delivery_service.sent) == count


def test_catalog_change_during_save_rechecked_inside_original_lock(tmp_path, monkeypatch):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    submit(base, callback(base))
    form = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
            'model_name': 'race', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-new-key'}
    original = model_control_cards.execute_model_profile_operation
    def mutate_then_save(agent, operation, payload, **kwargs):
        if operation == 'add':
            with model_control_cards.model_profile_write_check(None):
                add(host, model_name='concurrent-write')
        return original(agent, operation, payload, **kwargs)
    monkeypatch.setattr(model_control_cards, 'execute_model_profile_operation', mutate_then_save)
    with pytest.raises(ValueError):
        submit(base, callback(base, decision='save', fields=form))
    assert [row['model_name'] for row in read_model_profiles(model_profiles_path(host.home_paths))['profiles'].values()] == ['concurrent-write']


def test_card_expiry_and_admin_binding_revocation(tmp_path, monkeypatch):
    base, _, scope, _, _ = setup(tmp_path, monkeypatch, admin=True)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base)
    monkeypatch.setattr(private_interaction_service, 'admin_channel_identity_for_request', lambda *_: None)
    with pytest.raises(ValueError):
        submit(base, body)
    monkeypatch.setattr(private_interaction_service, 'admin_channel_identity_for_request', lambda *_: object())
    monkeypatch.setattr('agent_py_agent.agent.user_space.interaction_refs.time.time', lambda: 10**12)
    with pytest.raises(ValueError):
        submit(base, body)


def test_adapter_routes_native_form_only_to_authenticated_control(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import private_controls
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    base, _, scope, _, _ = setup(tmp_path, monkeypatch)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base)
    captured = []
    monkeypatch.setattr(GatewayClientCredentials, 'headers', lambda _: {'Authorization': 'Bearer test-host-token'})
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps(submit(base, captured[-1])).encode()
    def send(request, **kwargs):
        captured.append(json.loads(request.data))
        assert request.full_url.endswith('/client/private-interaction')
        assert request.get_header('Authorization') == 'Bearer test-host-token'
        return Response()
    monkeypatch.setattr(private_controls, 'open_authenticated_request', send)
    adapter = SimpleNamespace(my_agent_home=str(tmp_path), _gateway_port=8420)
    result = private_controls.tool_approval_card_action(adapter, {'operator_open_id': 'ou_actor',
        'message_id': body['message_id'], 'chat_id': body['chat_id'],
        'value': {'kind': 'my_agent_model_configuration', 'reference': body['reference'], 'decision': 'add', 'profile_id': ''}})
    assert result['toast']['type'] == 'success'
    assert captured == [body]


def test_control_transport_uses_existing_group_target_resolution(monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    calls = []
    def send(method, path, body, token):
        calls.append((path, body))
        return {'code': 0, 'data': {'message_id': 'om_card', 'chat_id': 'oc_group'}}
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', send)
    adapter = SimpleNamespace(_get_tenant_access_token=lambda: 'synthetic-tenant-token')
    assert feishu._send_feishu_control_card(adapter, 'oc_group', {'elements': []}, idempotency_key='one')['message_id'] == 'om_card'
    assert calls[0][0].endswith('receive_id_type=chat_id')
    assert calls[0][1]['receive_id'] == 'oc_group'
    with pytest.raises(ValueError):
        feishu._send_feishu_control_card(adapter, 'unknown_target', {}, idempotency_key='two')


@pytest.mark.parametrize('group', [False, True])
def test_actual_catalog_stays_outside_owner_file_and_shell_reads(tmp_path, monkeypatch, group):
    import shlex

    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.tests.test_host_files_access import _registry, _shell
    base, _, _, host, _ = setup(tmp_path, monkeypatch, group=group)
    add(host, api_key='synthetic-catalog-secret')
    catalog = model_profiles_path(host.home_paths)
    monkeypatch.setenv('MY_AGENT_HOME', str(base.home_paths.root))
    policy = PathAccessPolicy.from_values(mode='full', owner_scope_root=host.home_paths.owner_home_dir,
                                          agent_home_root=base.home_paths.root)
    assert not policy.check(catalog).allowed
    workspace = host.home_paths.owner_home_dir / 'workspace'
    workspace.mkdir(exist_ok=True)
    tools = _registry(workspace, owner_scope=host.home_paths.owner_home_dir, roots=[workspace])
    result = tools['read_file'].execute({'path': str(catalog)})
    assert not result.ok and 'synthetic-catalog-secret' not in result.output
    shell = _shell({'main': host.home_paths.owner_home_dir, 'root': base.home_paths.root}, scoped=True)
    result = shell.execute({'command': 'cat ' + shlex.quote(str(catalog)), 'working_dir': str(workspace),
                            '__sandbox_write_roots': [str(workspace)]})
    assert not result.ok and 'synthetic-catalog-secret' not in result.output


@pytest.mark.parametrize('operation', ['select', 'set_default'])
def test_completed_selection_rejects_different_profile_payload(tmp_path, monkeypatch, operation):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch)
    first, _ = add(host, model_name='first')
    second, _ = add(host, model_name='second')
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    body = callback(base, decision=operation, fields={'profile_id': first})
    result = submit(base, body)
    assert submit(base, body) == result
    before = read_model_profiles(model_profiles_path(host.home_paths))
    before_thread = host.conversation_store.threads.load(thread.thread_id)
    with pytest.raises(ValueError, match='不同参数'):
        submit(base, {**body, 'fields': {'profile_id': second}})
    with pytest.raises(ValueError, match='不同参数'):
        submit(base, {**body, 'fields': {'profile_id': first, 'extra': 'unexpected'}})
    assert read_model_profiles(model_profiles_path(host.home_paths)) == before
    assert host.conversation_store.threads.load(thread.thread_id) == before_thread


@pytest.mark.parametrize('changed', ['api_key', 'model_name', 'api_base', 'model_backend', 'model_context_window_tokens'])
def test_completed_save_only_replays_identical_fields_without_secret_storage(tmp_path, monkeypatch, changed):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch, group=True)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    submit(base, callback(base))
    form = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
            'model_name': 'saved-model', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-replay-key'}
    body = callback(base, decision='save', fields=form)
    result = submit(base, body)
    reordered = dict(reversed(list(form.items())))
    assert submit(base, {**body, 'fields': reordered}) == result
    before = read_model_profiles(model_profiles_path(host.home_paths))
    with pytest.raises(ValueError, match='不同参数'):
        submit(base, {**body, 'fields': {**form, changed: 'different-logical-value'}})
    assert read_model_profiles(model_profiles_path(host.home_paths)) == before
    records = list(host.home_paths.owner_home_dir.rglob('model-*.json'))
    completed = [json.loads(path.read_text()) for path in records if json.loads(path.read_text()).get('mode') == 'form']
    assert len(completed) == 1 and len(completed[0]['submission_digest']) == 64
    assert 'fields' not in completed[0] and 'api_key' not in completed[0]
    for path in records:
        assert form['api_key'] not in path.read_text()
        assert 'different-logical-value' not in path.read_text()


@pytest.mark.parametrize(('group', 'admin', 'label'), [(True, False, '配置归属：当前群'),
                                                      (False, False, '配置归属：你本人'),
                                                      (False, True, '配置归属：你的管理员目录')])
def test_selection_and_private_form_scope_comes_from_original_owner(tmp_path, monkeypatch, group, admin, label):
    base, _, scope, _, _ = setup(tmp_path, monkeypatch, group=group, admin=admin)
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    selection = base.delivery_service.sent[-1][1]['elements'][0]['text']['content']
    assert label in selection
    submit(base, callback(base))
    form = base.delivery_service.sent[-1][1]['elements'][0]['text']['content']
    assert label in form
    assert base.delivery_service.sent[-1][0].target == 'ou_actor'
    if group:
        assert '模型供该群执行使用' in selection and '该群后续创建的会话' in form
        assert '配置归属：你本人' not in form
    elif admin:
        assert '共享发布和全局初始模型' in selection
    else:
        assert '本人的私人目录' in form and '本人后续创建的会话' in selection


def test_group_default_changes_only_original_group_future_threads(tmp_path, monkeypatch):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch, group=True)
    profile_id, _ = add(host, model_name='group-default')
    original_selection = host.conversation_store.threads.load(thread.thread_id).model_profile_id
    model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope)
    assert submit(base, callback(base, decision='set_default', fields={'profile_id': profile_id}))['ok']
    assert read_model_profiles(model_profiles_path(host.home_paths))['selected'] == profile_id
    assert host.conversation_store.threads.load(thread.thread_id).model_profile_id == original_selection
    future_scope = GatewayControlScope(user_id='ou_another_group_actor', channel='feishu', conversation_id='oc_group',
                                       metadata={'channel_chat_type': 'group'})
    future_host, future = model_profile_service._scoped_model_host(base, future_scope)
    listing = execute_model_profile_operation(future_host, 'list', {}, thread_id=future.thread_id)
    assert listing['selected'] == profile_id
    personal = model_control_cards.model_control_host(base, OwnerIdentity.provider_user('feishu', 'ou_actor'))
    assert read_model_profiles(model_profiles_path(personal.home_paths))['selected'] == 'default'
