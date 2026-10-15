"""群能力集成入口与机器人引用来源证明；全用合成 provider，不连接真实租户。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.command_catalog import (
    COMMAND_INDEX,
    CommandAccessContext,
    command_access_error,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.http_routes import match_gateway_http_route
from agent_py_agent.agent.scheduler import event_source_tool as sources
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_group_authorization import _allow, _case
from agent_py_agent.tests.test_im_execution_threads import fixture_agent


def test_group_controls_only_parse_explicit_slash_and_use_authenticated_observation_route():
    command = parse_conversation_control('/group allow monitors.create all-current')
    assert command.kind == 'group' and command.value == 'allow monitors.create all-current'
    assert parse_conversation_control('请授权 /group allow monitors.create') is None
    assert command_access_error(COMMAND_INDEX['group'], CommandAccessContext('local', True))
    route = match_gateway_http_route('POST', '/channel-observations')
    assert route.access_tier == 'credential' and route.handler_module == '.channel_observations'


@pytest.mark.parametrize('key', ['im_group_authorization_required', 'feishu_bot_subscriptions_enabled'])
def test_group_deployment_flags_have_real_defaults_and_reject_unknown_values(tmp_path, key):
    assert getattr(CapabilityConfig(), key) is True
    path = tmp_path / 'capability.yaml'
    path.write_text(f'{key}: false\n')
    assert getattr(load_capability_config(path), key) is False
    path.write_text(f'{key}: maybe\n')
    with pytest.raises(ValueError):
        load_capability_config(path)


def test_group_tool_create_requires_real_grant_before_any_execution_thread(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    tool = ScheduleTool(agent)
    params = {'action': 'create', 'name': 'synthetic', 'prompt': '合成巡检', 'schedule_kind': 'every', 'every_seconds': 600}
    before = [thread.thread_id for thread in agent.conversation_store.threads.list()]
    denied = tool.execute(params)
    assert not denied.ok and denied.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    assert denied.effect_outcome == 'not_started'
    assert agent.scheduler_repository.list_jobs()[0] == []
    assert [thread.thread_id for thread in agent.conversation_store.threads.list()] == before
    _allow(agent, raw, actor, subject=actor.actor_id)
    assert tool.execute(params).ok
    assert agent.scheduler_repository.list_jobs()[0][0]['source_thread_id'] == source.thread_id


# LLM: 原 home 与当前官方群事实都来自隔离 fixture，Get 消息替身只返回明确 app_id，不模拟真实租户验收。
# 函数用途: 记录工具实际验证的精确消息，为跨群/跨应用伪造测试提供可控供应商响应。
def _event_case(tmp_path, monkeypatch):
    agent, _thread = fixture_agent(tmp_path, identity=OwnerIdentity.provider_group('feishu', 'oc_source'))
    scope = {'provider': 'feishu', 'chat_id': 'oc_source', 'app_id': 'cli_receiver', 'tenant_key': 'tenant',
        'bot_is_member': True, 'members_complete': True}
    seen = []
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_args: dict(scope))
    def resolve(_agent, chat, message):
        seen.append((chat, message))
        return {'app_id': 'cli_receiver', 'chat_id': chat, 'source_message_id': message,
            'sender_id': 'cli_other_bot', 'sender_id_type': 'app_id'}
    monkeypatch.setattr(sources, 'resolve_bot_source', resolve)
    return agent, scope, seen


def test_event_input_resolves_exact_other_bot_and_retains_proof(tmp_path, monkeypatch):
    agent, _scope, seen = _event_case(tmp_path, monkeypatch)
    result = sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one', 'om_two']})
    assert seen == [('oc_source', 'om_one'), ('oc_source', 'om_two')]
    assert result['source']['sender_id_type'] == 'app_id'
    assert result['source']['sender_ids'] == ['cli_other_bot']
    assert result['source']['proofs'] == [{'sender_id': 'cli_other_bot', 'source_message_id': 'om_one'}]
    sources.verify_event_rule_sources(agent, [{'schedule': result}])


@pytest.mark.parametrize('field', ['app_id', 'tenant_key', 'chat_id'])
def test_event_rules_cannot_use_model_supplied_other_scope(tmp_path, monkeypatch, field):
    agent, _scope, _seen = _event_case(tmp_path, monkeypatch)
    schedule = sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})
    schedule['source'][field] = 'other-scope'
    with pytest.raises(sources.EventSourceInputError, match='当前群'):
        sources.verify_event_rule_sources(agent, [{'schedule': schedule}])


def test_event_rule_cannot_forge_sender_or_enable_deployment_switch_locally(tmp_path, monkeypatch):
    agent, _scope, _seen = _event_case(tmp_path, monkeypatch)
    schedule = sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})
    schedule['source']['sender_ids'] = ['cli_forged']
    schedule['source']['proofs'][0]['sender_id'] = 'cli_forged'
    with pytest.raises(sources.EventSourceInputError, match='发送应用'):
        sources.verify_event_rule_sources(agent, [{'schedule': schedule}])
    monkeypatch.setattr(sources, 'deployment_capability_config', lambda _home: SimpleNamespace(feishu_bot_subscriptions_enabled=False))
    with pytest.raises(sources.EventSourceInputError, match='已关闭'):
        sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})


def test_source_network_preflight_does_not_hold_policy_lock_and_revoke_wins_final_write(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.user_space.group_authorization import (
        GroupAuthorizationChange,
        change_group_authorization,
    )
    from agent_py_agent.tests.test_group_authorization import _snapshot

    agent, _source, actor, raw, _calls = _case(tmp_path)
    saved = _allow(agent, raw, actor, subject=actor.actor_id)
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_args: dict(raw))
    def resolve(_agent, chat, message):
        with locked_json_path(agent.home_paths.owner_tool_policy_json, blocking=False):
            pass
        change_group_authorization(agent.home_paths, _snapshot(raw), actor,
            GroupAuthorizationChange('revoke', saved['revision'], grant_id=saved['grants'][0]['grant_id']))
        return {'app_id': raw['app_id'], 'chat_id': chat, 'source_message_id': message,
            'sender_id': 'other-bot', 'sender_id_type': 'app_id'}
    monkeypatch.setattr(sources, 'resolve_bot_source', resolve)
    before = [row.thread_id for row in agent.conversation_store.threads.list()]
    result = ScheduleTool(agent).execute({'action': 'create', 'name': 'synthetic subscription', 'prompt': '合成判断',
        'schedule_kind': 'event_source', 'event_source': {'source_message_ids': ['om_source']}})
    assert not result.ok and result.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    assert agent.scheduler_repository.list_jobs()[0] == []
    assert [row.thread_id for row in agent.conversation_store.threads.list()] == before
