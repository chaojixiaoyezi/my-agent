"""本人跨群来源复用原 OAuth/owner scheduler/Ingress；所有外部接口均为合成供应商。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.adapter import feishu_provider_facts as facts
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import channel_observations as endpoint
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = CapabilityConfig(feishu_user_oauth_enabled=True,
        feishu_oauth_redirect_uri='https://synthetic.invalid/feishu/oauth/callback', feishu_oauth_scopes=['im:chat:readonly'])
    monkeypatch.setattr(oauth, 'deployment_capability_config', lambda _home: config)
    agents = {}
    for actor in ('ou_a', 'ou_b'):
        agents[actor] = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
            my_agent_owner_provider='feishu', my_agent_owner_kind='user', my_agent_owner_id=actor,
            feishu_app_id='receiver', feishu_app_secret='synthetic-only'), tmp_path / actor)
    base = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
        feishu_app_id='receiver', feishu_app_secret='synthetic-only'), tmp_path / 'base')
    from agent_py_agent.tests.test_feishu_bot_observations import adapter
    current = adapter()
    current._resolve_bot_open_id = lambda: 'own-bot'
    service = SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda _channel: current))
    base.delivery_service = service
    for agent in agents.values():
        agent.delivery_service = service
    oauth_calls, rest_calls, leases = [], [], []
    members = ['ou_a', 'ou_b', 'ou_source']
    def api(_method, path, _body=None, token=''):
        oauth_calls.append(path)
        assert path.endswith('/user_info')
        return {'code': 0, 'data': {'open_id': token.removeprefix('access-')}}
    monkeypatch.setattr(oauth, '_oauth_api', api)
    for actor, agent in agents.items():
        row = {**oauth._configuration_facts(agent, create_key=True), 'actor_id': actor}
        row = oauth._ready_row(agent, row, {'access_token': 'access-' + actor, 'expires_in': 3600, 'scope': 'im:chat:readonly'})
        path = oauth.oauth_path(agent, actor)
        with oauth._credential_mutation(agent, path) as admission:
            oauth._save_credential_row(admission, path, row)
    def read(token, path, **_kwargs):
        rest_calls.append((token, path))
        if '/messages/' in path:
            return {'items': [{'message_id': 'om_source', 'chat_id': 'oc_group',
                'sender': {'sender_type': 'user', 'id_type': 'open_id', 'id': 'ou_source', 'tenant_key': 'tenant'}}]}
        if '/members/is_in_chat' in path:
            return {'is_in_chat': True}
        if '/members?' in path:
            return {'items': [{'member_id_type': 'open_id', 'member_id': actor} for actor in members], 'has_more': False}
        return {'chat_mode': 'group', 'tenant_key': 'tenant', 'owner_id_type': 'open_id', 'owner_id': 'ou_a'}
    monkeypatch.setattr(facts, '_read', read)
    @contextmanager
    def lease(owner):
        leases.append(('open', owner.owner_id))
        try:
            yield agents[owner.owner_id]
        finally:
            leases.append(('close', owner.owner_id))
    def resolve(_base, owner, *, lease_stack):
        assert owner.provider == 'feishu' and owner.owner_kind == 'user'
        return lease_stack.enter_context(lease(owner))
    monkeypatch.setattr(endpoint, '_resolve_request_agent_for_owner', resolve)
    jobs = {}
    for actor, agent in agents.items():
        source = {'channel': 'feishu', 'event_type': 'message', 'app_id': 'receiver', 'tenant_key': 'tenant',
            'chat_id': 'oc_group', 'sender_type': 'user', 'sender_id_type': 'open_id', 'sender_ids': ['ou_source'],
            'proofs': [{'sender_id': 'ou_source', 'source_message_id': 'om_source'}],
            'subscriber_actor_id': actor, 'authorization_source': 'personal_rule_oauth'}
        jobs[actor] = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='本人监控', prompt='合成条件',
            thread_id='thread-' + actor, schedule={'kind': 'event_source', 'source': source},
            creator={'channel': 'feishu', 'actor_id': actor}, source_request_id='create-' + actor,
            delivery={'mode': 'origin', 'reply_policy': 'explicit'}))[0]
        agent.scheduler_repository.list_jobs = Mock(side_effect=AssertionError('event must use exact index refs'))
    from agent_py_agent.agent.scheduler.source_owner_recovery import repair_source_owner
    for actor, agent in agents.items():
        repair_source_owner(base.scheduler_repository.due_index, base.home_paths.root,
            ('feishu', 'user', actor), agent.home_paths.owner_home_dir)
    base.scheduler_repository.due_index.mark_source_discovery_complete()
    event = {'channel': 'feishu', 'event_type': 'message', 'app_id': 'receiver', 'tenant_key': 'tenant',
        'chat_id': 'oc_group', 'message_id': 'om_source', 'sender_type': 'user', 'sender_id': 'ou_source',
        'sender_id_type': 'open_id', 'created_at': time.time(), 'content': 'synthetic untrusted content',
        'message_type': 'text', 'parent_id': '', 'root_id': '', 'thread_id': '', 'ingress_id': 'synthetic-ingress'}
    return SimpleNamespace(base=base, agents=agents, jobs=jobs, event=event, members=members,
        leases=leases, rest_calls=rest_calls, oauth_calls=oauth_calls, adapter=current, read=read)


def pending(case, actor):
    return case.agents[actor].scheduler_repository.get_job(case.jobs[actor]['job_id'])['event_source_state']['']['pending']


def test_personal_fanout_is_one_owner_per_slice_and_restart_replay_is_idempotent(case):
    first = endpoint.accept_channel_observation(case.base, case.event)
    assert first['status'] == 'partial' and first['source_owner_cursor'] == ['feishu', 'user', 'ou_a']
    assert len(pending(case, 'ou_a')) == 1 and pending(case, 'ou_b') == []
    second = endpoint.accept_channel_observation(case.base, case.event, source_owner_cursor=first['source_owner_cursor'])
    assert second['status'] == 'accepted' and len(pending(case, 'ou_b')) == 1
    replay = endpoint.accept_channel_observation(case.base, case.event)
    assert replay['status'] == 'partial' and len(pending(case, 'ou_a')) == 1
    assert case.leases == [('open', 'ou_a'), ('close', 'ou_a'), ('open', 'ou_b'), ('close', 'ou_b'), ('open', 'ou_a'), ('close', 'ou_a')]
    assert any(token == 'access-ou_a' for token, _path in case.rest_calls)
    assert any(token == 'access-ou_b' for token, _path in case.rest_calls)


@pytest.mark.parametrize('cause', ['left', 'revoked', 'tenant', 'app', 'network'])
def test_personal_rule_never_uses_group_authority_to_bypass_current_owner(case, monkeypatch, cause):
    agent = case.agents['ou_a']
    if cause == 'left':
        case.members.remove('ou_a')
    elif cause == 'revoked':
        path = oauth.oauth_path(agent, 'ou_a')
        from agent_py_agent.agent.common.json_io import read_json_object_report
        row = read_json_object_report(path).payload
        with oauth._credential_mutation(agent, path) as admission:
            oauth._save_credential_row(admission, path, {**row, 'status': 'revoked', 'ciphertext': ''})
    elif cause == 'tenant':
        original = case.read
        def changed(token, path, **kwargs):
            result = original(token, path, **kwargs)
            if 'chat_mode' in result:
                result['tenant_key'] = 'foreign'
            return result
        monkeypatch.setattr(facts, '_read', changed)
    elif cause == 'app':
        agent.config.feishu_app_id = 'foreign-app'
    else:
        agent.owner_policy = replace(agent.owner_policy, network_enabled=False)
    result = endpoint.accept_channel_observation(case.base, case.event)
    assert result['status'] in {'partial', 'ignored'}
    assert result['reason_code'] in {'EVENT_SOURCE_SUBSCRIBER_LEFT', 'EVENT_SOURCE_OAUTH_REVOKED',
        'EVENT_SOURCE_APPLICATION_CHANGED', 'EVENT_SOURCE_NETWORK_REVOKED', 'EVENT_SOURCE_NOT_SUBSCRIBED'}
    assert pending(case, 'ou_a') == [] and pending(case, 'ou_b') == []
    assert case.leases[-1] == ('close', 'ou_a')


def test_unsubscribe_removes_projection_without_touching_other_owner(case):
    repo = case.agents['ou_a'].scheduler_repository
    job = case.jobs['ou_a']
    repo.pause_job(job['job_id'], expected_version=job['version'])
    result = endpoint.accept_channel_observation(case.base, case.event)
    assert result['status'] == 'accepted'
    assert pending(case, 'ou_a') == [] and len(pending(case, 'ou_b')) == 1


@pytest.mark.parametrize('stamp', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_observation_time_is_rejected_before_owner_resolution(case, stamp):
    with pytest.raises(ValueError):
        endpoint._validated_observation({**case.event, 'created_at': stamp}, 'feishu')
    assert case.leases == []


def test_real_http_original_ingress_preserves_fanout_cursor_across_restart(case, tmp_path):
    from agent_py_agent.agent.adapter.manager import ChannelManager
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

    paths = gateway_paths(case.base)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=case.base,
        auth_middleware=AuthMiddleware(AuthManager())))
    server.start()
    def manager():
        value = ChannelManager(delivery_state_dir=tmp_path / 'transport')
        value._inbound_media_agent = case.base
        value.gateway_port = server.server.server_address[1]
        value._reply_poll_client = SimpleNamespace(credentials=SimpleNamespace(headers=lambda: authenticated_headers(case.base.home_paths.root)))
        value.register_adapter(case.adapter)
        return value
    try:
        first = manager()
        human = Mock()
        case.adapter.on_message(human)
        raw = {'header': {'app_id': 'receiver', 'tenant_key': 'tenant'}, 'event': {
            'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'ou_source'}}, 'message': {
                'chat_type': 'group', 'chat_id': 'oc_group', 'message_id': 'om_source', 'message_type': 'text',
                'content': 'synthetic', 'create_time': str(int(time.time() * 1000))}}}
        assert case.adapter._handle_feishu_event(raw).status == 'accepted'
        assert len(first._delivery_worker.store.pending()) == 1
        first._delivery_worker.run_once()
        records = first._delivery_worker.store.pending()
        # 原 worker 的每拍最多4阶段；A提交与cursor已持久，B可在后继阶段提交，但终态必须单独收口。
        assert len(pending(case, 'ou_a')) == 1
        if records:
            assert records[0].gateway_payload['source_owner_cursor'] == ['feishu', 'user', 'ou_a']
        first._delivery_worker.store.close()
        second = manager()
        for _ in range(4):
            second._delivery_worker.run_once()
            if not second._delivery_worker.store.pending():
                break
        assert second._delivery_worker.store.pending() == []
        assert len(pending(case, 'ou_a')) == len(pending(case, 'ou_b')) == 1
        human.assert_not_called()
        from urllib.error import HTTPError

        from agent_py_agent.agent.adapter.channel_observations import submit_channel_observation
        before = list(case.leases)
        for stamp in (float('nan'), float('inf'), float('-inf')):
            with pytest.raises(HTTPError) as rejected:
                submit_channel_observation(second, {**case.event, 'created_at': stamp})
            assert rejected.value.code == 400
            rejected.value.close()
        assert case.leases == before
        second._delivery_worker.store.close()
    finally:
        server.stop()


def test_personal_tool_creation_uses_real_active_context_oauth_and_exact_message(case):
    from agent_py_agent.agent.conversation.host_actor import actor_from_scope
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority

    agent = case.agents['ou_a']
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'ou_a', 'channel': 'feishu',
        'channel_conversation_id': 'ou_a', 'channel_user_id': 'ou_a', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_user_id='ou_a',
        channel_conversation_id='ou_a'), thread.thread_id, 'main-source')
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'source-task', 'goal': '合成订阅', 'status': 'active'},
        host_channel_actor=actor)
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': 'source-task', 'reason': 'thread_goal_continue'})
    params = _bind_background_authority(SimpleNamespace(agent=agent, store=store, thread=thread, claim=claim, task_id='source-task'))
    params.host_channel_actor = actor
    outcome = ScheduleTool(agent).execute({'action': 'create', 'name': '私人助手监控原群', 'prompt': '合成条件',
        'schedule_kind': 'event_source', 'event_source': {'source_message_ids': ['om_source']}})
    assert outcome.ok, outcome.output
    import json
    job = agent.scheduler_repository.get_job(json.loads(outcome.output)['job']['job_id'])
    assert job['owner']['id'] == agent.home_paths.owner_id
    assert job['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}
    assert job['schedule']['source']['chat_id'] == 'oc_group'
    assert job['schedule']['source']['subscriber_actor_id'] == 'ou_a'
    assert job['schedule']['source']['authorization_source'] == 'personal_rule_oauth'
    assert any(token == 'access-ou_a' and '/chats/oc_group?' in path for token, path in case.rest_calls)


def test_pause_during_oauth_group_read_prevents_pending_publication(case, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    original = case.read
    def read(token, path, **kwargs):
        if token == 'access-ou_a' and '/chats/oc_group?' in path:
            entered.set()
            assert release.wait(3)
        return original(token, path, **kwargs)
    monkeypatch.setattr(facts, '_read', read)
    errors, outcomes = [], []
    def consume():
        try:
            outcomes.append(endpoint.accept_channel_observation(case.base, case.event))
        except (ValueError, RuntimeError) as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert entered.wait(3)
        repo = case.agents['ou_a'].scheduler_repository
        job = repo.get_job(case.jobs['ou_a']['job_id'])
        repo.pause_job(job['job_id'], expected_version=job['version'])
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert outcomes[0]['reason_code'] == 'EVENT_SOURCE_RULE_RETIRED'
    assert pending(case, 'ou_a') == [] and pending(case, 'ou_b') == []


def test_unsubscribed_personal_source_is_constant_exact_negative_without_agent_or_rest(case):
    result = endpoint.accept_channel_observation(case.base, {**case.event, 'sender_id': 'ou_foreign'}, probe_only=True)
    assert result['status'] == 'ignored'
    assert case.leases == [] and case.rest_calls == []


def test_revoked_first_owner_advances_to_valid_second_without_acknowledging_unknown(case):
    from agent_py_agent.agent.common.json_io import read_json_object_report

    first = case.agents['ou_a']
    path = oauth.oauth_path(first, 'ou_a')
    row = read_json_object_report(path).payload
    with oauth._credential_mutation(first, path) as admission:
        oauth._save_credential_row(admission, path, {**row, 'status': 'revoked', 'ciphertext': ''})
    result = endpoint.accept_channel_observation(case.base, case.event)
    assert result['status'] == 'partial' and result['reason_code'] == 'EVENT_SOURCE_OAUTH_REVOKED'
    following = endpoint.accept_channel_observation(case.base, case.event, source_owner_cursor=result['source_owner_cursor'])
    assert following['status'] == 'accepted'
    assert pending(case, 'ou_a') == [] and len(pending(case, 'ou_b')) == 1


def test_pending_receipt_does_not_rebuild_or_enumerate_source_projection(case, monkeypatch):
    index = case.agents['ou_a'].scheduler_repository.due_index
    monkeypatch.setattr(index, 'prepare_source_jobs', Mock(side_effect=AssertionError('receipt is not definition')))
    monkeypatch.setattr(index, 'settle_source_jobs', Mock(side_effect=AssertionError('receipt is not definition')))
    monkeypatch.setattr(index, 'settle_source_owner', Mock(side_effect=AssertionError('receipt is not recovery')))
    result = endpoint.accept_channel_observation(case.base, case.event)
    assert result['status'] == 'partial' and len(pending(case, 'ou_a')) == 1
    assert endpoint.accept_channel_observation(case.base, case.event)['reason_code'] == 'EVENT_SOURCE_DUPLICATE'


def test_platform_uat_denial_advances_but_rate_limit_keeps_original_event_pending(case, monkeypatch):
    original = case.read
    code = [99991672]
    def read(token, path, **kwargs):
        if token == 'access-ou_a' and '/chats/oc_group?' in path:
            raise facts.FeishuProviderFactError('FEISHU_PROVIDER_SCOPE_REQUIRED' if code[0] != 429
                else 'FEISHU_PROVIDER_HTTP_REJECTED', code[0], stage='chat_info')
        return original(token, path, **kwargs)
    monkeypatch.setattr(facts, '_read', read)
    first = endpoint.accept_channel_observation(case.base, case.event)
    assert first['status'] == 'partial' and first['reason_code'] == 'EVENT_SOURCE_PROVIDER_AUTHORIZATION_DENIED'
    assert endpoint.accept_channel_observation(case.base, case.event, source_owner_cursor=first['source_owner_cursor'])['status'] == 'accepted'
    assert pending(case, 'ou_a') == [] and len(pending(case, 'ou_b')) == 1
    code[0] = 429
    with pytest.raises(facts.FeishuProviderFactError):
        endpoint.accept_channel_observation(case.base, {**case.event, 'message_id': 'om_source'})
    assert pending(case, 'ou_a') == []
