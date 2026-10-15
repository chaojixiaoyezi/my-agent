"""飞书机器人观察使用合成官方响应与原临时 Ingress，不启动服务或请求真实平台。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.adapter import feishu_provider_facts as facts
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_ws import lark_event_to_webhook_payload
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.gateway_parts.channel_observations import handle_channel_observation


def adapter():
    result = FeishuAdapter({'feishu_app_id': 'receiver', 'feishu_app_secret': 'synthetic-only',
        'feishu_session_lock_enabled': False})
    result._resolve_bot_open_id = lambda: 'receiver-open-id'
    result._get_tenant_access_token = lambda: 'synthetic-only'
    return result


def payload(sender_type='bot'):
    return {'header': {'app_id': 'receiver', 'tenant_key': 'receiver-tenant'}, 'event': {
        'sender': {'sender_type': sender_type, 'sender_id': {'open_id': 'source-open-id'}},
        'message': {'message_id': 'source-message', 'chat_id': 'group', 'chat_type': 'group',
            'message_type': 'text', 'create_time': '1000000', 'content': '{"text":"外部机器人内容"}'}}}


def host():
    return SimpleNamespace(delivery_service=SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda _channel: adapter())))


def provider_read(_token, path, **_kwargs):
    if '/messages/' in path:
        return {'items': [{'message_id': 'source-message', 'chat_id': 'group',
            'sender': {'sender_type': 'app', 'id_type': 'app_id', 'id': 'source-app', 'tenant_key': 'source-tenant'}}]}
    if '/members/is_in_chat' in path:
        return {'is_in_chat': True}
    if '/members?' in path:
        return {'items': [{'member_id_type': 'open_id', 'member_id': 'owner'},
            {'member_id_type': 'open_id', 'member_id': 'member'}], 'has_more': False}
    return {'chat_mode': 'group', 'chat_type': 'private', 'tenant_key': 'chat-tenant', 'owner_id': 'owner', 'owner_id_type': 'open_id',
        'user_manager_id_list': ['manager']}


def test_sdk_normalization_preserves_sender_kind_and_receiver_scope():
    data = SimpleNamespace(header=SimpleNamespace(app_id='receiver', tenant_key='tenant'), event=SimpleNamespace(
        sender=SimpleNamespace(sender_type='bot', sender_id=SimpleNamespace(open_id='bot-open'), tenant_key='source-tenant'),
        message=SimpleNamespace(message_id='message', chat_type='group', chat_id='chat', content='{}')))
    result = lark_event_to_webhook_payload(data)
    assert result['event']['sender']['sender_type'] == 'bot'
    assert result['header']['app_id'] == 'receiver' and result['header']['tenant_key'] == 'tenant'


@pytest.mark.parametrize('event_type', ['im.chat.member.bot.added_v1', 'im.chat.member.user.added_v1'])
def test_group_provider_hook_uses_same_chat_tenant_and_explicit_official_ids(tmp_path, monkeypatch, event_type):
    from agent_py_agent.agent.adapter import feishu_group_events as events
    from agent_py_agent.agent.user_space import group_authorization as authorization

    current = adapter()
    current.my_agent_home = str(tmp_path)
    saved = Mock()
    monkeypatch.setattr(events, '_read', lambda *_args: {'tenant_key': 'canonical-chat-tenant'})
    monkeypatch.setattr(authorization, 'record_group_provider_event', saved)
    raw = {'header': {'app_id': 'receiver', 'tenant_key': 'header-other-tenant', 'event_id': 'official-event',
        'event_type': event_type, 'create_time': '1730000000'},
        'event': {'chat_id': 'group', 'operator_id': {'open_id': 'official-operator'},
            'operator_tenant_key': 'external-inviter-tenant', 'users': [{'user_id': {'open_id': 'official-member'}}]}}
    assert events.admit_group_provider_event(current, raw).status == 'accepted'
    record = saved.call_args.args[1]
    assert record.provider == 'feishu' and record.app_id == 'receiver'
    assert record.tenant_key == 'canonical-chat-tenant' and record.operator_id == 'official-operator'
    assert record.member_ids == ('official-member',) and record.created_at == 1730000000


@pytest.mark.parametrize('event_type,accepted', [('im.chat.member.bot.deleted_v1', True),
    ('im.chat.member.user.deleted_v1', True), ('im.chat.member.bot.added_v1', False)])
def test_deleted_provider_hook_only_invalidates_prior_binding_after_chat_permission_loss(tmp_path, monkeypatch, event_type, accepted):
    from agent_py_agent.agent.adapter import feishu_group_events as events
    from agent_py_agent.agent.user_space import group_authorization as authorization

    current = adapter()
    current.my_agent_home = str(tmp_path)
    invalidated, authorized = Mock(), Mock()
    monkeypatch.setattr(events, '_read', Mock(side_effect=facts.FeishuProviderFactError('FEISHU_PROVIDER_PERMISSION_REQUIRED')))
    monkeypatch.setattr(authorization, 'record_group_provider_invalidation', invalidated)
    monkeypatch.setattr(authorization, 'record_group_provider_event', authorized)
    raw = {'header': {'app_id': 'receiver', 'event_id': 'official-event', 'event_type': event_type, 'create_time': '1730000000'},
        'event': {'chat_id': 'group', 'users': [{'user_id': {'open_id': 'official-member'}}]}}
    assert events.admit_group_provider_event(current, raw).status == ('accepted' if accepted else 'retryable')
    assert invalidated.call_count == int(accepted)
    if accepted:
        assert invalidated.call_args.args[1].tenant_key == ''
    authorized.assert_not_called()


@pytest.mark.parametrize('kind', ['bot', 'app'])
def test_bot_without_mention_never_reaches_human_commands_or_actor(kind):
    current = adapter()
    observed, human = [], Mock()
    current.on_observation(lambda event: observed.append(event))
    current.on_message(human)
    result = current._handle_feishu_event(payload(kind))
    assert result.status == 'accepted' and len(observed) == 1
    assert observed[0]['sender_id_type'] == 'open_id' and 'user_id' not in observed[0]
    human.assert_not_called()


@pytest.mark.parametrize('kind', ['', 'unknown', 'anonymous', 'user'])
def test_unknown_or_unmentioned_human_is_not_bot_subscription_input(kind):
    current = adapter()
    observed = Mock()
    current.on_observation(observed)
    assert current._handle_feishu_event(payload(kind)).status == 'ignored'
    observed.assert_not_called()


def test_self_bot_and_cross_receiver_are_rejected_before_observation():
    current = adapter()
    observed = Mock()
    current.on_observation(observed)
    raw = payload()
    raw['event']['sender']['sender_id']['open_id'] = 'receiver-open-id'
    assert current._handle_feishu_event(raw).reason_code == 'FEISHU_BOT_LOOP_SUPPRESSED'
    raw = payload()
    raw['header']['app_id'] = 'foreign'
    assert current._handle_feishu_event(raw).status == 'ignored'
    observed.assert_not_called()


def test_observation_uses_original_ingress_without_human_watcher(tmp_path, monkeypatch):
    manager = ChannelManager(delivery_state_dir=tmp_path / 'transport')
    manager._inbound_media_agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path / 'home'))
    current = adapter()
    manager.register_adapter(current)
    assert current._handle_feishu_event(payload()).status == 'accepted'
    assert current._handle_feishu_event(payload()).status == 'accepted'
    rows = manager._delivery_worker.store.pending()
    assert len(rows) == 1 and rows[0].kind == 'observation' and rows[0].user_id == ''
    sent = []
    monkeypatch.setattr('agent_py_agent.agent.adapter.channel_observations.submit_channel_observation',
        lambda _manager, body: sent.append(body) or {'status': 'accepted', 'reason_code': 'EVENT_SOURCE_REGISTERED'})
    manager._delivery_worker.run_once()
    assert len(sent) == 1 and sent[0]['ingress_id'] == rows[0].ingress_id
    assert manager._delivery_worker.store.pending() == [] and manager._reply_delivery.store.pending() == []


def test_source_get_keeps_app_id_distinct_from_event_open_id(monkeypatch):
    monkeypatch.setattr(facts, '_read', provider_read)
    proof = facts.resolve_bot_source(host(), 'group', 'source-message')
    assert proof['sender_id'] == 'source-app' and proof['app_id'] == 'receiver'
    assert proof['sender_id_type'] == 'app_id' and proof['source_message_id'] == 'source-message'
    event = {'app_id': 'receiver', 'tenant_key': 'receiver-tenant', 'chat_id': 'group',
        'message_id': 'source-message', 'sender_type': 'bot', 'sender_id': 'source-open-id'}
    result = facts.canonical_bot_observation(host(), event)
    assert result['sender_id'] == 'source-app' and result['sender_id'] != event['sender_id']
    assert result['tenant_key'] == 'chat-tenant' and result['provider_tenant_key'] == 'receiver-tenant'


def test_exact_get_rejects_cross_chat_and_human_sender(monkeypatch):
    monkeypatch.setattr(facts, '_read', provider_read)
    with pytest.raises(facts.FeishuProviderFactError):
        facts.resolve_bot_source(host(), 'other-group', 'source-message')
    monkeypatch.setattr(facts, '_read', lambda *_a, **_kw: {'items': [{'message_id': 'source-message',
        'chat_id': 'group', 'sender': {'sender_type': 'user', 'id_type': 'open_id', 'id': 'human'}}]})
    with pytest.raises(facts.FeishuProviderFactError):
        facts.resolve_bot_source(host(), 'group', 'source-message')


def test_snapshot_proves_complete_human_members_separately_from_bot_membership(monkeypatch):
    monkeypatch.setattr(facts, '_read', provider_read)
    snapshot = adapter().group_authority_snapshot('group')
    assert snapshot['provider'] == 'feishu' and snapshot['tenant_key'] == 'chat-tenant'
    assert snapshot['member_ids'] == ['member', 'owner'] and snapshot['members_complete'] is True
    assert snapshot['bot_is_member'] is True and snapshot['owner_id'] == 'owner'


def test_complete_members_exposes_optional_names_without_another_provider_query(monkeypatch):
    paths = []

    def read(token, path, **kwargs):
        paths.append(path)
        result = provider_read(token, path, **kwargs)
        if '/members?' in path:
            result['items'][0]['name'] = '合成同名'
            result['items'][1]['name'] = '  合成成员  '
        return result

    monkeypatch.setattr(facts, '_read', read)
    snapshot = adapter().group_authority_snapshot('group')
    assert snapshot['member_display_names'] == {'owner': '合成同名', 'member': '合成成员'}
    assert snapshot['member_ids'] == ['member', 'owner']
    assert len(paths) == 3 and not any('/contact/' in path for path in paths)


@pytest.mark.parametrize('name', [None, '', '  ', {'untrusted': '不是姓名'}, 42])
def test_missing_or_invalid_member_name_does_not_change_membership(monkeypatch, name):
    def read(token, path, **kwargs):
        result = provider_read(token, path, **kwargs)
        if '/members?' in path:
            result['items'][0]['name'] = name
        return result

    monkeypatch.setattr(facts, '_read', read)
    snapshot = adapter().group_authority_snapshot('group')
    assert snapshot['member_display_names'] == {}
    assert snapshot['member_ids'] == ['member', 'owner'] and snapshot['members_complete'] is True


@pytest.mark.parametrize('identity_type', ['app_id', 'user_id', 'union_id'])
def test_optional_name_does_not_turn_non_open_id_into_a_human_member(monkeypatch, identity_type):
    def read(token, path, **kwargs):
        result = provider_read(token, path, **kwargs)
        if '/members?' in path:
            result['items'][0].update(member_id_type=identity_type, name='合成名字')
        return result

    monkeypatch.setattr(facts, '_read', read)
    with pytest.raises(facts.FeishuProviderFactError, match='FEISHU_GROUP_MEMBERS_INCOMPLETE'):
        adapter().group_authority_snapshot('group')


def test_member_names_accumulate_only_with_complete_original_pagination(monkeypatch):
    calls = []

    def read(token, path, **kwargs):
        calls.append(path)
        if '/members?' in path:
            second = 'page_token=second' in path
            return {'items': [{'member_id_type': 'open_id', 'member_id': 'member' if second else 'owner',
                'name': '合成成员' if second else '合成群主'}], 'has_more': not second, 'page_token': '' if second else 'second'}
        return provider_read(token, path, **kwargs)

    monkeypatch.setattr(facts, '_read', read)
    snapshot = adapter().group_authority_snapshot('group')
    assert snapshot['member_display_names'] == {'owner': '合成群主', 'member': '合成成员'}
    assert snapshot['members_complete'] is True and len(calls) == 4


@pytest.mark.parametrize('chat_type', ['private', 'public'])
@pytest.mark.parametrize('chat_mode', ['group', 'topic'])
def test_group_chat_mode_accepts_private_and_public_visibility(monkeypatch, chat_type, chat_mode):
    def read(token, path, **kwargs):
        response = provider_read(token, path, **kwargs)
        if response.get('chat_mode'):
            response['chat_type'] = chat_type
            response['chat_mode'] = chat_mode
            response.pop('user_manager_id_list', None)
        return response
    monkeypatch.setattr(facts, '_read', read)
    assert adapter().group_authority_snapshot('group')['bot_is_member'] is True


def test_p2p_chat_mode_cannot_be_a_group_authority(monkeypatch):
    def read(token, path, **kwargs):
        response = provider_read(token, path, **kwargs)
        if response.get('chat_mode'):
            response['chat_mode'] = 'p2p'
        return response
    monkeypatch.setattr(facts, '_read', read)
    with pytest.raises(facts.FeishuProviderFactError):
        adapter().group_authority_snapshot('group')


def test_incomplete_member_pagination_cannot_authorize(monkeypatch):
    def read(token, path, **kwargs):
        if '/members?' in path:
            return {'items': [], 'has_more': True, 'page_token': 'same-token'}
        return provider_read(token, path, **kwargs)
    monkeypatch.setattr(facts, '_read', read)
    with pytest.raises(facts.FeishuProviderFactError, match='FEISHU_GROUP_MEMBERS_INCOMPLETE'):
        adapter().group_authority_snapshot('group')


def test_observation_http_requires_real_credential_before_reading_body():
    handler = SimpleNamespace(_auth_middleware=SimpleNamespace(has_gateway_credential=lambda _headers: False),
        headers={}, _read_json=Mock(), _send_json=Mock())
    handle_channel_observation(handler, SimpleNamespace(agent=object()))
    assert handler._send_json.call_args.args[0] == 403
    handler._read_json.assert_not_called()
