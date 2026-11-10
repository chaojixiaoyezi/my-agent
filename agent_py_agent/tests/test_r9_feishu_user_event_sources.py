"""指定成员只沿真实订阅、官方事实和原持久观察入口；不发模型请求。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.adapter import channel_observations as transport
from agent_py_agent.agent.adapter import feishu_provider_facts as facts
from agent_py_agent.agent.gateway_parts import channel_observations as endpoint
from agent_py_agent.agent.scheduler.event_sources import normalize_event_schedule
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRepository
from agent_py_agent.tests.test_feishu_bot_observations import adapter, host, payload, provider_read
from agent_py_agent.tests.test_scheduler_event_sources import event, schedule


def user_schedule():
    value = schedule()
    value['source'].update(app_id='receiver', chat_id='group', sender_type='user', sender_id_type='open_id',
        sender_ids=['member'], proofs=[{'sender_id': 'member', 'source_message_id': 'source-message'}])
    return normalize_event_schedule(value)


def observation():
    return {**event(app_id='receiver', chat_id='group', sender_type='user', sender_id_type='open_id',
        sender_id='member', message_id='source-message', created_at=time.time()),
        'message_type': 'text', 'parent_id': '', 'root_id': '', 'thread_id': ''}


def user_read(token, path, **kwargs):
    result = provider_read(token, path, **kwargs)
    if '/messages/' in path:
        result['items'][0]['sender'] = {'sender_type': 'user', 'id_type': 'open_id', 'id': 'member', 'tenant_key': 'chat-tenant'}
    return result


def repo_with_job(tmp_path):
    repo = SchedulerRepository(tmp_path / 'scheduler', owner_provider='feishu', owner_kind='group', owner_id='feishu/groups/group')
    job = repo.create_job(SchedulerJobCreateRequest(name='指定成员', prompt='按原条件研判', thread_id='thread-source',
        schedule=user_schedule(), creator={'channel': 'feishu', 'actor_id': 'owner'}, source_request_id='synthetic',
        delivery={'mode': 'origin', 'reply_policy': 'explicit'}))[0]
    return repo, job


def test_exact_user_schedule_reuses_original_replay_and_cancellation(tmp_path):
    repo, job = repo_with_job(tmp_path)
    incoming = observation()
    assert repo.accept_source_event(incoming)['reason_code'] == 'EVENT_SOURCE_REGISTERED'
    reopened = SchedulerRepository(repo.root, owner_provider='feishu', owner_kind='group', owner_id='feishu/groups/group')
    assert reopened.accept_source_event(incoming)['reason_code'] == 'EVENT_SOURCE_DUPLICATE'
    run = reopened.reserve_event_runs()[0]
    assert run['event_batch'] == [incoming]
    assert run['creator'] == job['creator']


@pytest.mark.parametrize('patch', [dict(sender_id='other'), dict(chat_id='other'), dict(app_id='other'),
    dict(tenant_key='other'), dict(sender_id_type='union_id'), dict(sender_type='bot')])
def test_foreign_user_event_never_enters_pending(tmp_path, patch):
    repo, job = repo_with_job(tmp_path)
    assert repo.accept_source_event({**observation(), **patch})['status'] == 'ignored'
    assert repo.get_job(job['job_id'])['event_source_state']['']['pending'] == []


def test_user_official_get_and_complete_membership_are_required(monkeypatch):
    monkeypatch.setattr(facts, '_read', user_read)
    value = facts.canonical_user_observation(host(), observation())
    assert value['sender_id'] == 'member' and value['tenant_key'] == 'chat-tenant'
    with pytest.raises(facts.FeishuProviderFactError):
        facts.canonical_user_observation(host(), {**observation(), 'sender_id': 'nickname'})
    def incomplete(token, path, **kwargs):
        value = user_read(token, path, **kwargs)
        if '/members?' in path:
            return {'items': [], 'has_more': True, 'page_token': 'repeated'}
        return value
    monkeypatch.setattr(facts, '_read', incomplete)
    with pytest.raises(facts.FeishuProviderFactError, match='FEISHU_GROUP_MEMBERS_INCOMPLETE'):
        facts.canonical_user_observation(host(), observation())


def test_unmentioned_user_uses_observation_and_ignored_never_becomes_conversation():
    current = adapter()
    human = Mock()
    current.on_message(human)
    current.on_observation(lambda _event: {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'})
    assert current._handle_feishu_event(payload('user')).status == 'ignored'
    current.on_observation(lambda _event: True)
    assert current._handle_feishu_event(payload('user')).status == 'accepted'
    human.assert_not_called()


def test_unsubscribed_human_never_enters_durable_ingress(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.manager import ChannelManager
    manager = ChannelManager(delivery_state_dir=tmp_path / 'transport')
    manager._inbound_media_agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path / 'home'))
    current = adapter()
    manager.register_adapter(current)
    monkeypatch.setattr(transport, 'submit_channel_observation', lambda *_a, **_kw:
        {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'})
    assert current._handle_feishu_event(payload('user')).status == 'ignored'
    assert manager._delivery_worker.store.pending() == []


def test_real_observation_http_probe_register_replay_and_lease_release(tmp_path, monkeypatch):
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

    base = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    paths = gateway_paths(base)
    owner = resolve_owner_home(base.home_paths.root, OwnerIdentity.provider_group('feishu', 'group'))
    repo, job = repo_with_job(tmp_path)
    owner.scheduler_dir.mkdir(parents=True, exist_ok=True)
    (owner.scheduler_dir / 'store.json').write_bytes(repo.store_path.read_bytes())
    scoped = host()
    scoped.scheduler_repository = repo
    leases = []
    @contextmanager
    def lease():
        leases.append('open')
        try:
            yield scoped
        finally:
            leases.append('close')
    def resolve(_base, _owner, *, lease_stack):
        assert _owner == OwnerIdentity.provider_group('feishu', 'group')
        return lease_stack.enter_context(lease())
    monkeypatch.setattr(endpoint, '_resolve_request_agent_for_owner', resolve)
    base.delivery_service = scoped.delivery_service
    monkeypatch.setattr(facts, '_read', user_read)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=base,
        auth_middleware=AuthMiddleware(AuthManager())))
    try:
        server.start()
        manager = SimpleNamespace(gateway_port=server.server.server_address[1],
            _reply_poll_client=SimpleNamespace(credentials=SimpleNamespace(headers=lambda: authenticated_headers(base.home_paths.root))))
        body = observation()
        assert transport.submit_channel_observation(manager, body, probe_only=True)['reason_code'] == 'EVENT_SOURCE_INTEREST_ONLY'
        assert repo.get_job(job['job_id'])['event_source_state']['']['pending'] == []
        assert transport.submit_channel_observation(manager, body)['reason_code'] == 'EVENT_SOURCE_REGISTERED'
        assert transport.submit_channel_observation(manager, body)['reason_code'] == 'EVENT_SOURCE_DUPLICATE'
        monkeypatch.setattr(facts, '_read', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('synthetic')))
        with pytest.raises(OSError):
            transport.submit_channel_observation(manager, body)
        assert leases == ['open', 'close'] * 4
    finally:
        server.stop()


def test_schedule_tool_creates_verified_user_source_under_original_group_grant(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler import event_source_tool as sources
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.tests.test_group_authorization import _allow, _case

    agent, _thread, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, subject=actor.actor_id)
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_args: raw)
    monkeypatch.setattr(facts, '_provider', lambda _agent: (SimpleNamespace(app_id=raw['app_id']), 'synthetic-only'))
    def read(_token, path, **_kwargs):
        assert '/messages/om_exact' in path
        return {'items': [{'message_id': 'om_exact', 'chat_id': raw['chat_id'], 'sender': {
            'sender_type': 'user', 'id_type': 'open_id', 'id': actor.actor_id, 'tenant_key': raw['tenant_key']}}]}
    monkeypatch.setattr(facts, '_read', read)
    outcome = ScheduleTool(agent).execute({'action': 'create', 'name': '指定成员', 'prompt': '合成条件',
        'schedule_kind': 'event_source', 'event_source': {'source_message_ids': ['om_exact']}})
    assert outcome.ok, outcome.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    assert job['schedule']['source']['sender_ids'] == [actor.actor_id]
    assert job['schedule']['source']['sender_type'] == 'user'
    assert job['creator'] == {'channel': 'feishu', 'actor_id': actor.actor_id}


def test_user_capability_default_and_strict_false_are_live_deployment_values(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
    from agent_py_agent.agent.settings.live_switches import DEPLOY_LIVE_SWITCH_KEYS

    assert CapabilityConfig().feishu_user_subscriptions_enabled is True
    assert 'feishu_user_subscriptions_enabled' in DEPLOY_LIVE_SWITCH_KEYS
    path = tmp_path / 'capability.yaml'
    path.write_text('feishu_user_subscriptions_enabled: false\n')
    assert load_capability_config(path).feishu_user_subscriptions_enabled is False
    path.write_text('feishu_user_subscriptions_enabled: maybe\n')
    with pytest.raises(ValueError):
        load_capability_config(path)
