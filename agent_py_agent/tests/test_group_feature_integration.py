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
    agent._current_run_params.source = 'gateway'
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


@pytest.mark.parametrize('field', ['app_id', 'tenant_key', 'chat_id'])
def test_event_rules_cannot_use_model_supplied_other_scope(tmp_path, monkeypatch, field):
    agent, _scope, _seen = _event_case(tmp_path, monkeypatch)
    schedule = sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})
    schedule['source'][field] = 'other-scope'
    with pytest.raises(ValueError, match='event_source 参数无效'):
        sources.prepare_event_sources(agent, {'rules': [{'schedule': schedule}]})


def test_event_rule_cannot_forge_sender_or_enable_deployment_switch_locally(tmp_path, monkeypatch):
    agent, _scope, _seen = _event_case(tmp_path, monkeypatch)
    schedule = sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})
    schedule['source']['sender_ids'] = ['cli_forged']
    schedule['source']['proofs'][0]['sender_id'] = 'cli_forged'
    with pytest.raises(ValueError, match='event_source 参数无效'):
        sources.prepare_event_sources(agent, {'rules': [{'schedule': schedule}]})
    monkeypatch.setattr(sources, 'deployment_capability_config', lambda _home: SimpleNamespace(feishu_bot_subscriptions_enabled=False))
    with pytest.raises(sources.EventSourceInputError, match='已关闭'):
        sources.schedule_from_event_input(agent, {'source_message_ids': ['om_one']})


@pytest.mark.parametrize('topic', [False, True])
def test_source_network_preflight_does_not_hold_policy_lock_and_revoke_wins_final_write(tmp_path, monkeypatch, topic):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.user_space.group_authorization import (
        GroupAuthorizationChange,
        change_group_authorization,
    )
    from agent_py_agent.tests.test_group_authorization import _snapshot

    agent, _source, actor, raw, _calls = _case(tmp_path,
        conversation_id='oc_source:thread:om_topic' if topic else 'oc_source')
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


def test_event_model_schema_exposes_references_for_top_level_and_each_rule():
    from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
    from agent_py_agent.agent.scheduler.tool import build_schedule_tool_model_spec

    properties = build_schedule_tool_model_spec().input_schema['properties']
    top = properties['event_source']['properties']
    nested = properties['rules']['items']['properties']['schedule']['properties']
    assert 'source_message_refs' in top and 'source_message_refs' in nested
    assert 'source' not in nested
    assert not {'app_id', 'tenant_key', 'sender_ids', 'proofs'} & set(top)
    base = {'action': 'create', 'rules': [{'id': 'one', 'name': '事件规则', 'use_llm': True,
        'prompt': '分析资料', 'schedule': {'kind': 'event_source'}}]}
    assert not validate_tool_input(base, build_schedule_tool_model_spec().input_schema).ok
    base['rules'][0]['schedule']['source_message_refs'] = ['opaque']
    assert validate_tool_input(base, build_schedule_tool_model_spec().input_schema).ok
    base['rules'][0]['schedule']['source_message_ids'] = ['om_one']
    assert not validate_tool_input(base, build_schedule_tool_model_spec().input_schema).ok


def test_topic_group_delivery_projects_only_transport_address():
    from agent_py_agent.agent.conversation.channels import binding_delivery_target

    topic = 'oc_source:thread:om_topic'
    assert binding_delivery_target('feishu', 'ou_member', topic, group=True) == 'oc_source'
    assert binding_delivery_target('feishu', 'ou_member', topic, group=False) == 'ou_member'


@pytest.mark.parametrize('target', ['oc_source:thread:om_topic', 'oc_source:thread:omt_topic',
    'oc_source:thread:', 'oc_source:other:om_topic', 'oc_source/other'])
def test_raw_topic_or_malformed_receive_id_is_rejected_before_transport(target):
    from agent_py_agent.agent.adapter.feishu import _message_send_path
    from agent_py_agent.agent.delivery.registry import _validate_feishu_target

    assert not _validate_feishu_target('feishu', target).allowed
    with pytest.raises(ValueError, match='CHANNEL_TARGET_INVALID'):
        _message_send_path(target)


def test_topic_subscription_proof_background_route_and_provider_use_exact_base_group(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.adapter import feishu
    from agent_py_agent.agent.adapter.protocol import provider_message_reference
    from agent_py_agent.agent.conversation.background_routing import (
        BackgroundRouteDependencies,
        resolve_background_route,
    )
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryContext,
        DeliveryService,
        ReplyEnvelope,
    )

    topic = 'oc_source:thread:om_topic'
    agent, source, actor, raw, _ = _case(tmp_path, conversation_id=topic)
    _allow(agent, raw, actor, subject=actor.actor_id)
    agent._current_run_params.source = 'gateway'
    reference = provider_message_reference('om_source')
    agent._current_run_params.host_message_references = ((reference, 'om_source'),)
    seen, sent = [], []
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_: dict(raw))

    # LLM: 仅替代原官方Get传输，记录精确basechat+消息；原schema/授权/规则与持久化均为真实入口。
    # 函数用途: 为合成来源证明返回同应用同群的其他机器人身份。
    def get_source(_agent, chat, message):
        seen.append((chat, message))
        return {'app_id': raw['app_id'], 'chat_id': chat, 'source_message_id': message,
            'sender_id': 'cli_other_bot', 'sender_id_type': 'app_id'}

    monkeypatch.setattr(sources, 'resolve_bot_source', get_source)
    result = ScheduleTool(agent).execute({'action': 'create', 'name': '合成订阅', 'prompt': '合成一行',
        'schedule_kind': 'event_source', 'event_source': {'source_message_refs': [reference]}, 'delivery': {'mode': 'origin'}})
    assert result.ok, result.output
    job = agent.scheduler_repository.get_job(json.loads(result.output)['job']['job_id'])
    assert seen == [('oc_source', 'om_source')] and job['source_thread_id'] == source.thread_id
    assert agent._current_run_params.host_channel_actor == actor and actor.conversation_id == topic
    channel, target = resolve_background_route(BackgroundRouteDependencies(agent.conversation_store.threads.load,
        lambda: (), lambda: ('', '')), job['thread_id'])
    assert (channel, target) == ('feishu', 'oc_source')

    # LLM: 仅替代平台HTTP出口，不补投递结果账；真实Delivery/adapter正文构造必须给准确供应商receive_id。
    # 函数用途: 记录最终物理群地址并返回供应商成功形状，无网络。
    def provider(method, path, body, _token):
        sent.append((method, path, body['receive_id']))
        return {'code': 0}

    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', provider)
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', SimpleNamespace(send_message=lambda target, message:
        feishu._send_feishu_rendered(target, message.content, 'synthetic-token')),
        capabilities=ChannelCapabilities(text=True, proactive=True))
    receipt = DeliveryService(registry).deliver(DeliveryContext(channel=channel, target=target, mode='proactive'),
        ReplyEnvelope(content='合成一行'))
    assert receipt.delivery_status == 'sent'
    assert sent == [('POST', '/im/v1/messages?receive_id_type=chat_id', 'oc_source')]


def test_nested_event_public_input_resolves_host_reference_before_canonical_rules(tmp_path, monkeypatch):
    from copy import deepcopy

    from agent_py_agent.agent.adapter.protocol import provider_message_reference

    agent, _scope, seen = _event_case(tmp_path, monkeypatch)
    reference = provider_message_reference('om_one')
    agent._current_run_params.host_message_references = ((reference, 'om_one'),)
    raw = {'rules': [{'id': 'one', 'name': '原消息事件', 'use_llm': True, 'prompt': '分析合成资料',
        'schedule': {'kind': 'event_source', 'source_message_refs': [reference], 'min_interval_seconds': 60}}]}
    before = deepcopy(raw)
    prepared = sources.prepare_event_sources(agent, raw)
    assert raw == before and seen == [('oc_source', 'om_one')]
    assert prepared.rules[0]['schedule']['source']['proofs'] == [
        {'sender_id': 'cli_other_bot', 'source_message_id': 'om_one'}]
    assert 'source_message_refs' not in prepared.rules[0]['schedule']


def test_top_level_event_reference_never_reaches_provider_as_diagnostic_hash(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.protocol import provider_message_reference

    agent, _scope, seen = _event_case(tmp_path, monkeypatch)
    reference = provider_message_reference('om_one')
    agent._current_run_params.host_message_references = ((reference, 'om_one'),)
    result = sources.schedule_from_event_input(agent, {'source_message_refs': [reference]})
    assert seen == [('oc_source', 'om_one')]
    assert result['source']['proofs'][0]['source_message_id'] == 'om_one'


@pytest.mark.parametrize('fault', ['unknown', 'other_request', 'attrs_only', 'raw_hash', 'both', 'foreign_owner'])
def test_event_reference_scope_failures_never_query_provider(tmp_path, monkeypatch, fault):
    from dataclasses import replace

    from agent_py_agent.agent.adapter.protocol import provider_message_reference

    agent, _scope, seen = _event_case(tmp_path, monkeypatch)
    reference = provider_message_reference('om_one')
    agent._current_run_params.host_message_references = ((reference, 'om_one'),)
    raw = {'source_message_refs': [reference]}
    if fault == 'unknown':
        raw['source_message_refs'] = ['not-from-this-message']
    elif fault == 'other_request':
        agent._current_run_params.request_id = 'another-request'
    elif fault == 'attrs_only':
        agent._current_run_params.host_message_references = ()
        agent._current_run_params.task_attributes['host_message_references'] = ((reference, 'om_one'),)
    elif fault == 'raw_hash':
        raw = {'source_message_ids': [reference]}
    elif fault == 'both':
        raw['source_message_ids'] = ['om_one']
    else:
        actor = agent._current_run_params.host_channel_actor
        agent._current_run_params.host_channel_actor = replace(actor, owner_id='providers/feishu/groups/foreign')
    with pytest.raises(ValueError):
        sources.schedule_from_event_input(agent, raw)
    assert not seen and agent.scheduler_repository.list_jobs()[0] == []


@pytest.mark.parametrize('count', [1, 3])
def test_fake_model_tool_calls_replay_quoted_refs_into_original_scheduler(tmp_path, monkeypatch, count):
    import json
    from copy import deepcopy

    from agent_py_agent.agent.adapter.feishu_quoted_media import (
        ValidatedQuotedMedia,
        append_quoted_text,
    )
    from agent_py_agent.agent.adapter.manager import _gateway_ask_payload
    from agent_py_agent.agent.adapter.protocol import IncomingMessage
    from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_message_references
    from agent_py_agent.agent.scheduler.tool import build_schedule_tool_model_spec

    agent, thread, actor, scope, _calls = _case(tmp_path)
    agent._current_run_params.source = 'gateway'
    _allow(agent, scope, actor, subject=actor.actor_id)
    msg = IncomingMessage(channel='feishu', user_id=actor.actor_id, message_id='om_current',
        content='订阅我引用的机器人消息', conversation_id='oc_source',
        metadata={'feishu_chat_type': 'group', 'feishu_chat_id': 'oc_source', 'feishu_parent_id': 'om_one'})
    append_quoted_text(msg, ValidatedQuotedMedia('om_current', 'om_one', 'oc_source', (), '合成外部资料', 6, ('om_one',)))
    payload = _gateway_ask_payload(msg)
    agent._current_run_params.host_message_references = _gateway_message_references(
        {'metadata': payload['metadata']}, SimpleNamespace(scope=SimpleNamespace(channel='feishu')))
    reference = agent._current_run_params.host_message_references[0][0]
    seen = []
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_args: dict(scope))
    def resolve(_agent, chat, message):
        seen.append(message)
        return {'app_id': scope['app_id'], 'chat_id': chat, 'source_message_id': message,
            'sender_id': 'other-bot', 'sender_id_type': 'app_id'}
    monkeypatch.setattr(sources, 'resolve_bot_source', resolve)
    model_call = {'action': 'create', 'name': '合成消息研判', 'delivery': {'mode': 'origin'},
        'rules': [{'id': f'rule_{i}', 'name': f'规则{i}', 'use_llm': True, 'prompt': '依原需求分析外部资料',
            'schedule': {'kind': 'event_source', 'source_message_refs': [reference], 'min_interval_seconds': 60}}
            for i in range(count)]}
    before = deepcopy(model_call)
    assert validate_tool_input(model_call, build_schedule_tool_model_spec().input_schema).ok
    tool = ScheduleTool(agent)
    created = tool.execute(model_call)
    assert created.ok, created.output
    first = json.loads(created.output)['job']['job_id']
    assert model_call == before and seen == ['om_one'] * count
    # 同一模型调用重投仍由原 source_request_id/job 指纹负责幂等，不另设引用或订阅账。
    replayed = tool.execute(model_call)
    assert replayed.ok and json.loads(replayed.output)['job']['job_id'] == first
    jobs, errors = agent.scheduler_repository.list_jobs()
    assert not errors and len(jobs) == 1 and jobs[0]['source_thread_id'] == thread.thread_id
    assert len(jobs[0]['rules']) == count
    for rule in jobs[0]['rules']:
        assert rule['schedule']['source']['proofs'] == [{'sender_id': 'other-bot', 'source_message_id': 'om_one'}]
        assert 'source_message_refs' not in rule['schedule']


@pytest.mark.parametrize('fault', ['group', 'message', 'ref', 'duplicate', 'canonical_source'])
def test_inbound_quote_mapping_rejects_conflicts_before_tool_dispatch(tmp_path, monkeypatch, fault):
    from copy import deepcopy

    from agent_py_agent.agent.adapter.protocol import provider_message_reference
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_message_references

    mapping = {'schema': 'quoted_message_references.v1', 'channel': 'feishu', 'chat_id': 'oc_source',
        'current_message_id': 'om_current', 'references': [{'ref': provider_message_reference('om_one'), 'message_id': 'om_one'}]}
    metadata = {'channel_chat_type': 'group', 'channel_chat_id': 'oc_source', 'message_id': 'om_current',
        'quoted_message_references': mapping}
    if fault == 'group':
        mapping['chat_id'] = 'oc_foreign'
    elif fault == 'message':
        mapping['current_message_id'] = 'om_foreign'
    elif fault == 'ref':
        mapping['references'][0]['ref'] = 'not-the-original-ref'
    elif fault == 'duplicate':
        mapping['references'] *= 2
    else:
        mapping['source'] = {'app_id': 'model-cannot-supply-this'}
    before = deepcopy(metadata)
    with pytest.raises(ValueError):
        _gateway_message_references({'metadata': metadata}, SimpleNamespace(scope=SimpleNamespace(channel='feishu')))
    assert metadata == before


def test_gateway_run_params_carries_only_this_authenticated_request_references(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.protocol import provider_message_reference
    from agent_py_agent.agent.gateway_parts import request_execution as gateway
    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationContext,
    )

    agent, thread, actor, _raw, _calls = _case(tmp_path)
    reference = provider_message_reference('om_one')
    request = {'id': actor.entry_request_id, 'metadata': {'channel_chat_type': 'group',
        'channel_chat_id': 'oc_source', 'message_id': 'om_current', 'quoted_message_references': {
            'schema': 'quoted_message_references.v1', 'channel': 'feishu', 'chat_id': 'oc_source',
            'current_message_id': 'om_current', 'references': [{'ref': reference, 'message_id': 'om_one'}]}}}
    conversation = GatewayConversationContext(thread_id=thread.thread_id, scope=SimpleNamespace(
        channel='feishu', channel_conversation_id='oc_source', channel_user_id=actor.actor_id))
    context = GatewayAskRunContext(agent, request, tmp_path / 'request.json', tmp_path / 'response.json',
        actor.entry_request_id, lambda _chunk: None)
    monkeypatch.setattr(gateway.request_prompt, 'gateway_injections', lambda *_args: [])
    monkeypatch.setattr(gateway.request_prompt, 'gateway_conversation_history_seed', lambda *_args, **_kwargs: None)
    params = gateway._gateway_run_params(gateway._GatewayRunParamsRequest(request, context, conversation, '只给引用'))
    assert params.host_message_references == ((reference, 'om_one'),)
    assert params.host_channel_actor == actor
    assert 'host_message_references' not in (params.task_attributes or {})
    request['metadata'].pop('quoted_message_references')
    next_params = gateway._gateway_run_params(gateway._GatewayRunParamsRequest(request, context, conversation, '下次请求'))
    assert next_params.host_message_references == ()


@pytest.mark.parametrize('field,value', [('app_id', 'foreign'), ('chat_id', 'foreign'),
    ('sender_id_type', 'open_id'), ('source_message_id', 'om_foreign'), ('sender_id', 'cli_receiver')])
def test_resolved_reference_still_requires_exact_current_provider_proof(tmp_path, monkeypatch, field, value):
    from agent_py_agent.agent.adapter.protocol import provider_message_reference

    agent, _scope, _seen = _event_case(tmp_path, monkeypatch)
    reference = provider_message_reference('om_one')
    agent._current_run_params.host_message_references = ((reference, 'om_one'),)
    def forged(*_args):
        return {'app_id': 'cli_receiver', 'chat_id': 'oc_source', 'sender_id': 'cli_other_bot',
            'sender_id_type': 'app_id', 'source_message_id': 'om_one', field: value}
    monkeypatch.setattr(sources, 'resolve_bot_source', forged)
    with pytest.raises(ValueError):
        sources.schedule_from_event_input(agent, {'source_message_refs': [reference]})
    assert agent.scheduler_repository.list_jobs()[0] == []
