"""群能力只用合成 provider 事实；不连接飞书、不读取真实配置或代替真人点击。"""
from __future__ import annotations

import contextvars
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.user_space.group_authorization import (
    GroupAuthoritySnapshot,
    GroupAuthorizationChange,
    GroupAuthorizationError,
    GroupProviderEvent,
    change_group_authorization,
    read_group_authorization,
    record_group_provider_event,
)
from agent_py_agent.agent.user_space.group_authorization_context import group_capability_guard
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


# LLM: 使用原线程/HostActor 工厂，只将 provider 查询替换为明确合成元数据，不写真实授权或发消息。
# 函数用途: 为角色、成员、CAS 与子孙上下文验证提供一个隔离群。
def _case(tmp_path):
    agent, source = fixture_agent(tmp_path, identity=OwnerIdentity('feishu', 'group', 'oc_source'))
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_user_id='ou_owner',
        channel_conversation_id='oc_source'), source.thread_id, 'synthetic-request')
    agent._current_run_params = RunParams(request_id='synthetic-request', host_channel_actor=actor,
        task_attributes={'conversation_thread_id': source.thread_id})
    raw = {'provider': 'feishu', 'chat_id': 'oc_source', 'app_id': 'app_synthetic', 'tenant_key': 'tenant_synthetic',
        'owner_id': 'ou_owner', 'member_ids': ['ou_owner', 'ou_inviter', 'ou_member'],
        'members_complete': True, 'bot_is_member': True}
    calls = []
    def snapshot(_chat):
        calls.append(_chat)
        return dict(raw)
    adapter = SimpleNamespace(group_authority_snapshot=snapshot)
    agent.delivery_service = SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda channel: adapter))
    return agent, source, actor, raw, calls


def _snapshot(raw):
    return GroupAuthoritySnapshot.from_mapping(raw)


def _allow(agent, raw, actor, *, capability='monitors.create', subject='ou_member', all_current=False):
    revision = read_group_authorization(agent.home_paths)[1]['revision']
    return change_group_authorization(agent.home_paths, _snapshot(raw), actor,
        GroupAuthorizationChange('allow', revision, capability, '' if all_current else subject, all_current))


def _event(*, event_type='im.chat.member.user.deleted_v1', event_id='event-one', members=('ou_member',), operator='', created_at=10):
    return GroupProviderEvent('feishu', 'oc_source', 'app_synthetic', 'tenant_synthetic', event_id,
        event_type, created_at, operator, members)


@pytest.mark.parametrize('actor_id', ['ou_owner', 'ou_inviter', 'ou_member'])
def test_every_group_actor_needs_explicit_capability_even_with_auto_mode(tmp_path, actor_id):
    agent, source, actor, raw, _calls = _case(tmp_path)
    actor = replace(actor, actor_id=actor_id)
    agent._current_run_params.host_channel_actor = actor
    agent.config.access_mode = 'full-access'
    with pytest.raises(GroupAuthorizationError, match='尚未获得') as failure:
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pytest.fail('未授权不可执行副作用')
    assert failure.value.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    assert not read_group_authorization(agent.home_paths)[1]['grants']


def test_owner_explicit_exact_grant_only_enables_named_member_and_capability(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    saved = _allow(agent, raw, actor)
    assert saved['grants'][0]['subjects'] == {'ou_member': 0}
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
        pass
    for capability, other in [('assistant.configure', 'ou_member'), ('monitors.create', 'ou_inviter')]:
        agent._current_run_params.host_channel_actor = replace(actor, actor_id=other)
        with pytest.raises(GroupAuthorizationError):
            with group_capability_guard(agent, capability, thread_id=source.thread_id):
                pytest.fail('不能扩大能力或成员')


def test_ordinary_member_cannot_grant_self_even_after_configuration_grant(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, capability='assistant.configure')
    member = replace(actor, actor_id='ou_member')
    with pytest.raises(GroupAuthorizationError) as failure:
        _allow(agent, raw, member)
    assert failure.value.error_code == 'GROUP_AUTHORIZATION_NOT_AUTHORITY'


def test_old_group_does_not_guess_inviter_but_real_bot_added_can_prove_current_inviter(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    inviter = replace(actor, actor_id='ou_inviter')
    with pytest.raises(GroupAuthorizationError):
        _allow(agent, raw, inviter)
    event = _event(event_type='im.chat.member.bot.added_v1', operator='ou_inviter')
    first = record_group_provider_event(agent.home_paths.root, event)
    assert record_group_provider_event(agent.home_paths, event) == first
    assert _allow(agent, raw, inviter)['grants'][0]['authority'] == 'bot_inviter'


def test_all_current_is_snapshot_and_new_member_is_not_automatically_granted(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, all_current=True)
    raw['member_ids'].append('ou_new')
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_new')
    with pytest.raises(GroupAuthorizationError) as failure:
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pytest.fail('后来加入成员未获授权')
    assert failure.value.error_code == 'GROUP_AUTHORIZATION_REQUIRED'


def test_member_departure_reentry_and_owner_reauthorization(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor)
    member = replace(actor, actor_id='ou_member')
    agent._current_run_params.host_channel_actor = member
    record_group_provider_event(agent.home_paths, _event())
    raw['member_ids'].remove('ou_member')
    with pytest.raises(GroupAuthorizationError) as absent:
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pass
    assert absent.value.error_code == 'GROUP_AUTHORIZATION_MEMBER_REQUIRED'
    raw['member_ids'].append('ou_member')
    record_group_provider_event(agent.home_paths, _event(event_type='im.chat.member.user.added_v1', event_id='rejoin'))
    with pytest.raises(GroupAuthorizationError) as stale:
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pass
    assert stale.value.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    _allow(agent, raw, actor)
    with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
        pass


def test_inviter_departure_and_bot_reentry_do_not_restore_old_authority_or_grants(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    record_group_provider_event(agent.home_paths, _event(event_type='im.chat.member.bot.added_v1', operator='ou_inviter'))
    _allow(agent, raw, replace(actor, actor_id='ou_inviter'))
    record_group_provider_event(agent.home_paths, _event(event_id='inviter-left', members=('ou_inviter',)))
    with pytest.raises(GroupAuthorizationError):
        _allow(agent, raw, replace(actor, actor_id='ou_inviter'))
    record_group_provider_event(agent.home_paths, _event(event_type='im.chat.member.bot.added_v1', event_id='new-bot', operator='ou_member', created_at=20))
    record_group_provider_event(agent.home_paths, _event(event_type='im.chat.member.bot.added_v1', event_id='late-old', operator='ou_inviter', created_at=1))
    block = read_group_authorization(agent.home_paths)[1]
    assert block['inviter']['actor_id'] == 'ou_member'
    assert block['grants'][0]['group_epoch'] != block['group_epoch']


def test_cas_revoke_and_expired_copied_context_cannot_reuse_lease(tmp_path):
    agent, source, actor, raw, calls = _case(tmp_path)
    saved = _allow(agent, raw, actor)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
        copied = contextvars.copy_context()
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            assert len(calls) == 1
    with pytest.raises(GroupAuthorizationError) as conflict:
        change_group_authorization(agent.home_paths, _snapshot(raw), actor,
            GroupAuthorizationChange('revoke', 0, grant_id=saved['grants'][0]['grant_id']))
    assert conflict.value.error_code == 'GROUP_AUTHORIZATION_CONFLICT'
    change_group_authorization(agent.home_paths, _snapshot(raw), actor,
        GroupAuthorizationChange('revoke', saved['revision'], grant_id=saved['grants'][0]['grant_id']))
    def retry():
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pytest.fail('复制的旧租约已过期')
    with pytest.raises(GroupAuthorizationError):
        copied.run(retry)


@pytest.mark.parametrize('field,value', [('members_complete', False), ('bot_is_member', False), ('owner_id', ''), ('member_ids', None)])
def test_provider_missing_or_incomplete_facts_fail_closed(tmp_path, field, value):
    agent, source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    raw[field] = value
    with pytest.raises(GroupAuthorizationError) as failure:
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pass
    assert failure.value.error_code == 'GROUP_AUTHORITY_UNAVAILABLE'


def test_missing_flag_on_fake_config_does_not_disable_boundary(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import group_authorization_context as context
    agent, source, _actor, _raw, _calls = _case(tmp_path)
    monkeypatch.setattr(context, 'deployment_capability_config', lambda _: SimpleNamespace())
    with pytest.raises(GroupAuthorizationError):
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pytest.fail('缺 flag 不可放行')


# LLM: 老调度测试显式建立隔离群授权前提，不关闭安全开关或改变产品默认；provider 只用合成完整事实。
# 函数用途: 让原 creator/取消测试继续验证既有边界，并复用真正的人工授权写入和原 tool_policy 锁。
def grant_synthetic_group_monitor_capability(agent, actor, *, members, capability="monitors.create"):
    raw = {'provider': agent.home_paths.owner_provider, 'chat_id': actor.conversation_id,
        'app_id': 'app_synthetic', 'tenant_key': 'tenant_synthetic', 'owner_id': 'ou_fixture_owner',
        'member_ids': sorted(set(members) | {'ou_fixture_owner'}), 'members_complete': True, 'bot_is_member': True}
    adapter = SimpleNamespace(group_authority_snapshot=lambda chat: dict(raw))
    delivery = getattr(agent, 'delivery_service', None) or SimpleNamespace()
    registry = getattr(delivery, 'registry', None)
    if registry is None:
        delivery.registry = SimpleNamespace(adapter_for=lambda channel: adapter)
    else:
        original = registry.adapter_for
        def adapter_for(channel):
            found = original(channel)
            if channel == raw['provider']:
                found = found or adapter
                found.group_authority_snapshot = lambda chat: dict(raw)
            return found
        registry.adapter_for = adapter_for
    agent.delivery_service = delivery
    owner = replace(actor, actor_id='ou_fixture_owner')
    _allow(agent, raw, owner, all_current=True, capability=capability)


def test_active_copied_context_cannot_borrow_parent_lease_across_thread_or_task(tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.user_space.group_authorization_context import (
        require_group_mutation_lease,
    )

    agent, source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, subject=actor.actor_id)
    def denied():
        with pytest.raises(GroupAuthorizationError):
            with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
                pytest.fail('并行上下文不得借原锁')
        with pytest.raises(GroupAuthorizationError):
            require_group_mutation_lease(agent.home_paths, 'monitors.create')
    with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contextvars.copy_context().run, denied).result(timeout=3)
    async def check_tasks():
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            async def child():
                denied()
            await asyncio.create_task(child())
    asyncio.run(check_tasks())


def test_equal_timestamp_old_bot_added_cannot_restore_inviter_after_dedup_eviction(tmp_path):
    agent, _source, _actor, _raw, _calls = _case(tmp_path)
    old = _event(event_type='im.chat.member.bot.added_v1', event_id='old-added', operator='ou_inviter', created_at=1000)
    record_group_provider_event(agent.home_paths, old)
    record_group_provider_event(agent.home_paths, replace(old, event_type='im.chat.member.bot.deleted_v1', event_id='deleted'))
    record_group_provider_event(agent.home_paths, replace(old, event_id='new-added', operator_id='ou_member'))
    for index in range(128):
        record_group_provider_event(agent.home_paths, _event(event_id=f'member-{index}', created_at=1001 + index))
    record_group_provider_event(agent.home_paths, old)
    assert read_group_authorization(agent.home_paths)[1]['inviter'] is None


def test_bot_removed_without_rest_access_only_invalidates_existing_exact_app(tmp_path):
    from agent_py_agent.agent.user_space.group_authorization import (
        record_group_provider_invalidation,
    )

    agent, source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, subject=actor.actor_id)
    removal = _event(event_type='im.chat.member.bot.deleted_v1', members=())
    before = read_group_authorization(agent.home_paths)[1]
    record_group_provider_invalidation(agent.home_paths, replace(removal, tenant_key='', app_id='unrelated-app'))
    assert read_group_authorization(agent.home_paths)[1] == before
    record_group_provider_invalidation(agent.home_paths, replace(removal, tenant_key=''))
    with pytest.raises(GroupAuthorizationError):
        with group_capability_guard(agent, 'monitors.create', thread_id=source.thread_id):
            pass
    with pytest.raises(GroupAuthorizationError):
        record_group_provider_invalidation(agent.home_paths, replace(removal, event_type='im.chat.member.bot.added_v1'))


def test_child_without_own_thread_cannot_borrow_parent_group_permission(tmp_path):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent, _source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, subject=actor.actor_id)
    previous = set_current_subagent_context(agent, run_id='synthetic-child', task_attributes={})
    try:
        with pytest.raises(GroupAuthorizationError):
            with group_capability_guard(agent, 'monitors.create'):
                pytest.fail('child 没有原线程，不能退回父参数')
    finally:
        restore_current_subagent_context(agent, previous)
