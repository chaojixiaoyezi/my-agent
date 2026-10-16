"""群创建者姓名刷新使用原索引及合成成员HTTP，不读取真实配置或连接飞书。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.scheduler.creator_access import creator_display_rows
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


# LLM: 只构造原owner布局与实际REST适配器，网络及token均由测试显式替换，不创建真实用户。
# 函数用途: 为群/私人/跨provider展示提供同一隔离索引与成员调用记录。
def display_case(tmp_path, monkeypatch, *, kind='group', provider='feishu'):
    identity = OwnerIdentity(provider, kind, 'oc_synthetic' if kind == 'group' else 'ou_synthetic')
    home = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, identity))
    adapter = FeishuAdapter({'feishu_app_id': 'synthetic', 'feishu_app_secret': 'synthetic-only'})
    adapter._get_tenant_access_token = lambda: 'synthetic-only'
    agent = SimpleNamespace(home_paths=home, delivery_service=SimpleNamespace(
        registry=SimpleNamespace(adapter_for=lambda channel: adapter)))
    calls = []

    def read(token, path, **kwargs):
        calls.append((path, kwargs))
        return {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_a', 'name': '同名'},
            {'member_id_type': 'open_id', 'member_id': 'ou_b', 'name': '同名'}], 'has_more': False}

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_provider_facts._read', read)
    return agent, adapter, calls


def test_missing_group_creator_names_refresh_once_and_preserve_creator(tmp_path, monkeypatch):
    agent, _, calls = display_case(tmp_path, monkeypatch)
    jobs = [{'creator': {'channel': 'feishu', 'actor_id': actor}} for actor in ['ou_a', 'ou_b', 'ou_a']]
    rows = creator_display_rows(agent, jobs)
    assert [row['creator_display'] for row in rows] == [f'同名（feishu:{actor}）' for actor in ['ou_a', 'ou_b', 'ou_a']]
    assert [row['creator'] for row in rows] == [row['creator'] for row in jobs]
    assert len(calls) == 1 and calls[0][0].startswith('/im/v1/chats/oc_synthetic/members?')
    assert '/contact/' not in calls[0][0]
    assert creator_display_rows(agent, jobs) == rows and len(calls) == 1


@pytest.mark.parametrize('kind,provider,channel,jobs_empty', [
    ('user', 'feishu', 'feishu', False), ('group', 'other', 'feishu', False),
    ('group', 'feishu', 'other', False), ('group', 'feishu', 'feishu', True),
])
def test_private_foreign_provider_and_empty_queries_do_not_refresh(tmp_path, monkeypatch, kind, provider, channel, jobs_empty):
    agent, _, calls = display_case(tmp_path, monkeypatch, kind=kind, provider=provider)
    jobs = [] if jobs_empty else [{'creator': {'channel': channel, 'actor_id': 'ou_a'}}]
    creator_display_rows(agent, jobs)
    assert calls == [] and not agent.home_paths.provider_identity_dir.exists()


@pytest.mark.parametrize('fault', ['provider', 'chat', 'partial', 'member_type', 'empty_name', 'outside_member'])
def test_invalid_or_partial_display_snapshot_does_not_write_index(tmp_path, monkeypatch, fault):
    agent, adapter, _ = display_case(tmp_path, monkeypatch)
    snapshot = {'provider': 'feishu', 'chat_id': 'oc_synthetic', 'members_complete': True,
        'member_ids': ['ou_a'], 'member_display_names': {'ou_a': '合成'}}
    if fault == 'provider':
        snapshot['provider'] = 'other'
    elif fault == 'chat':
        snapshot['chat_id'] = 'oc_foreign'
    elif fault == 'partial':
        snapshot['members_complete'] = False
    elif fault == 'member_type':
        snapshot['member_ids'] = [42]
    elif fault == 'empty_name':
        snapshot['member_display_names'] = {'ou_a': '  '}
    else:
        snapshot['member_ids'] = ['ou_other']
    adapter.group_member_snapshot = lambda _: snapshot
    rows = creator_display_rows(agent, [{'creator': {'channel': 'feishu', 'actor_id': 'ou_a'}}])
    assert rows[0]['creator_display'] == '姓名未知（feishu:ou_a）'
    assert not agent.home_paths.provider_identity_dir.exists()


def test_complete_pagination_only_publishes_needed_creator_names(tmp_path, monkeypatch):
    agent, _, calls = display_case(tmp_path, monkeypatch)

    def read(token, path, **kwargs):
        calls.append((path, kwargs))
        second = 'page_token=next' in path
        return {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_b' if second else 'ou_a', 'name': '成员'}],
            'has_more': not second, 'page_token': '' if second else 'next'}

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_provider_facts._read', read)
    rows = creator_display_rows(agent, [{'creator': {'channel': 'feishu', 'actor_id': 'ou_b'}}])
    assert rows[0]['creator_display'] == '成员（feishu:ou_b）' and len(calls) == 2
    from agent_py_agent.agent.user_space.identity_store import lookup_provider_identity
    assert lookup_provider_identity(agent.home_paths, provider='feishu', provider_subject_id='ou_a') is None
    assert all(0 < entry[1]['timeout'] <= 2 for entry in calls)


@pytest.mark.parametrize('fault', ['wrong_type', 'incomplete', 'timeout'])
def test_original_member_parser_failure_keeps_unknown_and_no_partial_write(tmp_path, monkeypatch, caplog, fault):
    agent, _, calls = display_case(tmp_path, monkeypatch)

    def read(token, path, **kwargs):
        calls.append((path, kwargs))
        if fault == 'timeout':
            from agent_py_agent.agent.adapter.feishu_provider_facts import FeishuProviderFactError
            raise FeishuProviderFactError('FEISHU_PROVIDER_TRANSPORT_UNAVAILABLE', stage='members')
        return {'items': [{'member_id_type': 'union_id' if fault == 'wrong_type' else 'open_id',
            'member_id': 'ou_a', 'name': 'Synthetic-Name-Canary'}], 'has_more': True, 'page_token': 'same'}

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_provider_facts._read', read)
    rows = creator_display_rows(agent, [{'creator': {'channel': 'feishu', 'actor_id': 'ou_a'}}])
    assert rows[0]['creator_display'] == '姓名未知（feishu:ou_a）' and len(calls) <= 2
    assert not agent.home_paths.provider_identity_dir.exists()
    assert 'stage=members' in caplog.text and 'Synthetic-Name-Canary' not in caplog.text


def test_network_read_is_outside_identity_lock_and_existing_authority_fields_survive(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.common.json_io import locked_json_path

    agent, _, calls = display_case(tmp_path, monkeypatch)
    path = agent.home_paths.provider_identity_dir / 'feishu.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {'schema_version': 'provider-identity.v1', 'provider': 'feishu', 'provider_subject_id': 'ou_a',
        'owner_kind': 'main', 'owner_id': 'main', 'canonical_user_id': 'canonical_synthetic',
        'owner_home': 'original', 'display_name': '', 'policy_extension': {'same': True}}
    path.write_text(json.dumps(original) + '\n')

    def read(token, remote, **kwargs):
        with locked_json_path(path, blocking=False):
            calls.append(remote)
        return {'items': [{'member_id_type': 'open_id', 'member_id': 'ou_a', 'name': '成员'}], 'has_more': False}

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_provider_facts._read', read)
    creator_display_rows(agent, [{'creator': {'channel': 'feishu', 'actor_id': 'ou_a'}}])
    after = json.loads(path.read_text().splitlines()[-1])
    assert {key: value for key, value in after.items() if key not in {'display_name', 'updated_at'}} == {
        key: value for key, value in original.items() if key != 'display_name'}
    assert after['display_name'] == '成员' and len(calls) == 1


def test_busy_identity_lock_is_best_effort_and_does_not_delay_query(tmp_path, monkeypatch, caplog):
    import threading
    import time

    from agent_py_agent.agent.common.json_io import locked_json_path

    agent, _, calls = display_case(tmp_path, monkeypatch)
    path = agent.home_paths.provider_identity_dir / 'feishu.jsonl'
    held, release = threading.Event(), threading.Event()

    def hold():
        with locked_json_path(path):
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(1)
        started = time.monotonic()
        rows = creator_display_rows(agent, [{'creator': {'channel': 'feishu', 'actor_id': 'ou_a'}}])
        assert time.monotonic() - started < 1 and len(calls) == 1
        assert rows[0]['creator_display'] == '姓名未知（feishu:ou_a）'
        assert 'error_type=BlockingIOError' in caplog.text
    finally:
        release.set()
        thread.join(3)
    assert not path.exists()


@pytest.mark.parametrize('entry', ['slash', 'tool'])
def test_slash_and_model_schedule_query_share_current_group_name_refresh(tmp_path, monkeypatch, entry):
    import json

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, _, tool = group_case(tmp_path)
    job = create(tool)
    display, _, calls = display_case(tmp_path, monkeypatch)
    agent.delivery_service = display.delivery_service
    # 此夹具的原群是oc_source，资源只沿当前owner读取，不能沿另一构造器的群。
    if entry == 'slash':
        monkeypatch.setattr('agent_py_agent.agent.gateway_parts.control_service._request_agent_for_scope', lambda *_: agent)
        result = execute_schedule_control(agent, parse_conversation_control('/schedule'),
            GatewayControlScope('ou_a', 'feishu', 'oc_source', resolved_owner=OwnerIdentity.provider_group('feishu', 'oc_source')))
        assert result.ok and '同名（feishu:ou_a）' in result.message
    else:
        result = tool.execute({'action': 'list'})
        assert result.ok and json.loads(result.output)['jobs'][0]['creator_display'] == '同名（feishu:ou_a）'
    assert len(calls) == 1 and calls[0][0].startswith('/im/v1/chats/oc_source/members?')
    assert agent.scheduler_repository.get_job(job['job_id'])['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}


@pytest.mark.parametrize('action', ['create', 'update', 'pause', 'resume', 'cancel', 'delete'])
def test_successful_create_and_mutations_return_same_creator_display(tmp_path, monkeypatch, action):
    import json

    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, _, tool = group_case(tmp_path)
    calls = []
    agent.delivery_service.registry.adapter_for('feishu').group_member_snapshot = lambda _chat: calls.append(1)
    remember_provider_display_names(agent.home_paths, provider='feishu', names={'ou_a': '合成创建者'})
    job = create(tool)
    if action == 'create':
        result = job
    else:
        if action == 'resume':
            assert tool.execute({'action': 'pause', 'job_id': job['job_id'], 'expected_version': job['version']}).ok
        current = agent.scheduler_repository.get_job(job['job_id'])
        params = {'action': action, 'job_id': job['job_id'], 'expected_version': current['version']}
        if action == 'update':
            params['prompt'] = '更新合成正文'
        outcome = tool.execute(params)
        assert outcome.ok, (action, outcome.error_code)
        result = json.loads(outcome.output)['job']
    assert result['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}
    assert result['creator_display'] == '合成创建者（feishu:ou_a）'
    assert not calls  # 已有名字无需额外REST；展示不能把名字当作身份或扩充调用权限。


@pytest.mark.parametrize('action', ['list', 'get'])
def test_metadata_only_queries_still_exclude_creator_and_display(tmp_path, monkeypatch, action):
    import json

    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, _, tool = group_case(tmp_path)
    remember_provider_display_names(agent.home_paths, provider='feishu', names={'ou_a': 'Synthetic-Name-Canary'})
    job = create(tool)
    result = tool.execute({'action': action, 'job_id': job['job_id'], 'metadata_only': True})
    assert result.ok
    payload = json.loads(result.output)
    rows = payload['jobs'] if action == 'list' else [payload['job']]
    assert all('creator' not in row and 'creator_display' not in row for row in rows)
    assert 'Synthetic-Name-Canary' not in result.output


def test_create_name_refresh_runs_after_original_group_policy_lease_exits(tmp_path):
    from agent_py_agent.agent.user_space.group_authorization_context import _active_lease
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, _, tool = group_case(tmp_path)
    calls = []

    def snapshot(chat):
        assert _active_lease(agent.home_paths) is None
        calls.append(chat)
        return {'provider': 'feishu', 'chat_id': chat, 'members_complete': True,
            'member_ids': ['ou_a'], 'member_display_names': {'ou_a': '合成创建者'}}

    agent.delivery_service.registry.adapter_for('feishu').group_member_snapshot = snapshot
    job = create(tool)
    assert job['creator_display'] == '合成创建者（feishu:ou_a）' and calls == ['oc_source']
    assert job['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}


def test_display_enrichment_does_not_allow_group_notification_takeover(tmp_path):
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names
    from agent_py_agent.tests.test_scheduler_host_actor import create, group_case

    agent, _, _, tool = group_case(tmp_path)
    remember_provider_display_names(agent.home_paths, provider='feishu', names={'ou_a': '合成创建者'})
    job = create(tool)
    before = agent.scheduler_repository.store_path.read_bytes()
    result = tool.execute({'action': 'takeover', 'job_id': job['job_id'], 'expected_version': job['version']})
    assert not result.ok and result.error_code == 'CHANNEL_ACTOR_SCOPE_MISMATCH'
    assert agent.scheduler_repository.store_path.read_bytes() == before


def test_display_enrichment_only_replaces_output_fields(tmp_path):
    import json
    from dataclasses import asdict

    from agent_py_agent.agent.scheduler.tool import _with_creator_display
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.identity_store import remember_provider_display_names

    agent = SimpleNamespace(home_paths=home_paths(tmp_path))
    remember_provider_display_names(agent.home_paths, provider='feishu', names={'ou_a': '合成创建者'})
    outcome = ToolHandlerOutcome('schedule', True, json.dumps({'ok': True, 'action': 'update',
        'job': {'creator': {'channel': 'feishu', 'actor_id': 'ou_a'}}}), call_id='call_synthetic',
        handler_executed=True, effect_source_ref='original_effect', result_envelope={'keep': True})
    updated = _with_creator_display(agent, outcome)
    assert {key: value for key, value in asdict(updated).items() if key != 'output'} == {
        key: value for key, value in asdict(outcome).items() if key != 'output'}
    assert json.loads(updated.output)['job']['creator_display'] == '合成创建者（feishu:ou_a）'
