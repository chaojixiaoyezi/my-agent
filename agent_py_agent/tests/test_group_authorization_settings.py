"""真实设置/人格写入口的群授权回归；仅临时原账与合成 provider，无网络或真实凭据。"""
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
)
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.agent.user_space.group_authorization import GroupAuthorizationError
from agent_py_agent.agent.user_space.group_authorization_context import group_capability_guard
from agent_py_agent.tests.test_group_authorization import _allow, _case
from agent_py_agent.tests.test_model_profiles import profile


def test_group_model_write_and_selection_need_no_extra_monitor_grant(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    from agent_py_agent.agent.settings import AgentConfig

    agent.config = AgentConfig(model_backend='echo', my_agent_home=str(agent.home_paths.root))
    assert thread_model_profile_id(agent, source.thread_id) == 'default'
    model_id = str(uuid4())
    payload = {'profile_id': model_id, 'profile': profile()}
    assert execute_model_profile_operation(agent, 'add', payload, thread_id=source.thread_id)['ok']
    assert thread_model_profile_id(agent, source.thread_id, select=model_id) == model_id
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    assert thread_model_profile_id(agent, source.thread_id, select='default') == 'default'


def test_group_decision_patch_and_reset_both_gate_original_cas(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    current = execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)
    payload = {'changes': {'enabled': True}, 'expected_revision': current['revision']}
    saved = execute_decision_settings_operation(agent, 'patch', payload, thread_id=source.thread_id)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    reset = execute_decision_settings_operation(agent, 'reset', {'fields': ['enabled'], 'expected_revision': saved['revision']},
        thread_id=source.thread_id)
    assert reset['revision'] != saved['revision']
    assert execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)['revision'] == reset['revision']
    with pytest.raises(ValueError):
        execute_decision_settings_operation(agent, 'reset', {'fields': ['enabled'], 'expected_revision': saved['revision']},
            thread_id=source.thread_id)


def test_group_approval_policy_write_reuses_original_lock_and_preserves_grants(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    assert execute_approval_mode_operation(agent.home_paths, 'get')['ok']
    assert execute_approval_mode_operation(agent.home_paths, 'set', 'auto', authorization_host=agent)['ok']
    with group_capability_guard(agent, 'assistant.configure'):
        pass
    with pytest.raises(GroupAuthorizationError):
        execute_approval_mode_operation(agent.home_paths, 'set', 'ask')


def test_direct_group_persona_repository_cannot_skip_final_lease(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    repository = PersonaRepository.from_home_paths(agent.home_paths)
    request = PersonaMutationRequest(target='agents', action='add', content='合成工作约定', confirmed=True)
    agent.home_paths.owner_agents_md.parent.mkdir(parents=True, exist_ok=True)
    agent.home_paths.owner_agents_md.write_text('# AGENTS\n')
    before = agent.home_paths.owner_agents_md.read_bytes()
    with pytest.raises(GroupAuthorizationError):
        repository.mutate(request)
    assert agent.home_paths.owner_agents_md.read_bytes() == before
    with group_capability_guard(agent, 'assistant.configure'):
        assert repository.mutate(request)['entry_id']
    with pytest.raises(GroupAuthorizationError):
        repository.mutate(request)


def test_group_user_config_legacy_yaml_set_and_reset_remain_forbidden(tmp_path):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

    agent, _source, actor, raw, _calls = _case(tmp_path)
    tool = UserConfigTool(agent)
    for action in ('set', 'reset', 'revert'):
        outcome = tool.execute({'action': action, 'key': 'memory_compact_auto_trigger_percent', 'value': 80})
        assert not outcome.ok and outcome.error_code == 'TOOL_PERMISSION_DENIED'


@pytest.mark.parametrize('topic', [False, True])
def test_original_signed_group_model_form_is_independent_of_monitor_revocation(tmp_path, monkeypatch, topic):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.agent.user_space.group_authorization import (
        GroupAuthoritySnapshot,
        GroupAuthorizationChange,
        change_group_authorization,
        read_group_authorization,
    )
    from agent_py_agent.tests.test_im_model_cards import callback, setup, submit

    if topic:
        from agent_py_agent.tests import test_im_model_cards

        scope_type = test_im_model_cards.GatewayControlScope
        monkeypatch.setattr(test_im_model_cards, 'GatewayControlScope',
            lambda **kwargs: scope_type(**{**kwargs, 'conversation_id': 'oc_group:thread:om_topic'}))
    base, _owner, scope, host, _thread = setup(tmp_path, monkeypatch, group=True)
    snapshot = GroupAuthoritySnapshot('feishu', 'oc_group', 'synthetic-app', 'synthetic-tenant',
        'ou_actor', frozenset({'ou_actor'}))
    actor = host._current_run_params.host_channel_actor
    block = change_group_authorization(host.home_paths, snapshot, actor,
        GroupAuthorizationChange('allow', 0, 'monitors.create', group_wide=True))
    assert model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope).ok
    assert base.delivery_service.sent[0][0].target == 'oc_group'
    assert submit(base, callback(base))['ok']
    change_group_authorization(host.home_paths, snapshot, actor,
        GroupAuthorizationChange('revoke', block['revision'], grant_id=block['grants'][0]['grant_id']))
    assert read_group_authorization(host.home_paths)[1]['grants'][0]['status'] == 'revoked'
    form = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
        'model_name': 'synthetic', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-canary'}
    assert submit(base, callback(base, decision='save', fields=form))['ok']
    assert model_profiles_path(host.home_paths).exists()


def test_topic_model_card_private_form_save_keeps_exact_signed_source(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.tests import test_im_model_cards

    scope_type = test_im_model_cards.GatewayControlScope
    monkeypatch.setattr(test_im_model_cards, 'GatewayControlScope',
        lambda **kwargs: scope_type(**{**kwargs, 'conversation_id': 'oc_group:thread:om_topic'}))
    base, _, scope, host, thread = test_im_model_cards.setup(tmp_path, monkeypatch, group=True)
    assert model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope).ok
    assert base.delivery_service.sent[-1][0].target == 'oc_group'
    assert base.delivery_service.sent[-1][0].thread_id == thread.thread_id
    assert test_im_model_cards.submit(base, test_im_model_cards.callback(base))['ok']
    assert base.delivery_service.sent[-1][0].target == 'ou_actor'
    fields = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
        'model_name': 'synthetic-topic', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-canary'}
    result = test_im_model_cards.submit(base, test_im_model_cards.callback(base, decision='save', fields=fields))
    assert result['ok']
    assert len(json.loads(model_profiles_path(host.home_paths).read_text())['profiles']) == 1
    assert host._current_run_params.host_channel_actor.conversation_id == 'oc_group:thread:om_topic'
    assert host.conversation_store.threads.require(thread.thread_id).channel_bindings[0].channel_conversation_id == 'oc_group:thread:om_topic'


@pytest.mark.parametrize('conversation_id', ['oc_other:thread:om_topic', 'oc_group_extra:thread:om_topic',
    'oc_group:thread:', 'oc_group:thread:bad/topic', 'oc_group:thread:om_topic:extra'])
@pytest.mark.parametrize('metadata', [{}, {'channel_chat_type': 'group'}])
def test_model_card_invalid_or_cross_group_topic_has_zero_outbound(tmp_path, monkeypatch, conversation_id, metadata):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.tests.test_im_model_cards import setup

    base, _, scope, _, _ = setup(tmp_path, monkeypatch, group=True)
    result = model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'),
        replace(scope, conversation_id=conversation_id, metadata=metadata))
    assert not result.ok and base.delivery_service.sent == []


def test_group_model_card_uses_canonical_owner_when_chat_type_metadata_is_missing(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.tests.test_im_model_cards import setup

    base, _, scope, _, _ = setup(tmp_path, monkeypatch, group=True)
    result = model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'),
        replace(scope, conversation_id='oc_group:thread:future_topic', metadata={}))
    assert result.ok and base.delivery_service.sent[-1][0].target == 'oc_group'


@pytest.mark.parametrize('fault', ['ambiguous_binding', 'wrong_owner', 'wrong_actor'])
def test_signed_group_callback_does_not_guess_or_borrow_original_source(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.user_space.group_authorization_context import (
        bind_group_interaction_host,
    )
    from agent_py_agent.tests.test_im_model_cards import setup

    base, _, _, host, thread = setup(tmp_path, monkeypatch, group=True)
    payload = {'channel': 'feishu', 'thread_id': thread.thread_id, 'actor_id': 'ou_actor', 'control_id': 'synthetic'}
    before = host._current_run_params.host_channel_actor
    if fault == 'ambiguous_binding':
        host.conversation_store.threads.write(replace(thread, channel_bindings=(*thread.channel_bindings,
            replace(thread.channel_bindings[0], channel_conversation_id='oc_group:thread:future_topic'))))
    elif fault == 'wrong_owner':
        host.conversation_store.threads.write(replace(thread, owner_id='other-owner'))
    else:
        payload['actor_id'] = 'ou_other'
    with pytest.raises(GroupAuthorizationError):
        bind_group_interaction_host(base, host, payload, 'ou_actor')
    assert host._current_run_params.host_channel_actor == before and base.delivery_service.sent == []


def test_manual_oauth_rechecks_after_network_and_does_not_hold_policy_lock(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import model_oauth
    from agent_py_agent.agent.user_space.group_authorization import (
        GroupAuthorizationChange,
        change_group_authorization,
        read_group_authorization,
    )
    from agent_py_agent.agent.user_space.group_authorization_context import current_group_authority
    from agent_py_agent.tests.test_model_oauth import setup

    agent, _source, actor, raw, _calls = _case(tmp_path)
    setup(agent)
    _allow(agent, raw, actor, subject=actor.actor_id)
    def network(_auth):
        block = read_group_authorization(agent.home_paths)[1]
        change_group_authorization(agent.home_paths, current_group_authority(agent), actor,
            GroupAuthorizationChange('revoke', block['revision'], grant_id=block['grants'][0]['grant_id']))
        return {'device_code': 'synthetic-device', 'user_code': 'ABCD', 'verification_uri': 'https://example.test',
            'interval': 1, 'expires_at': 9999999999}
    monkeypatch.setattr(model_oauth, 'start_device', network)
    assert execute_model_profile_operation(agent, 'auth_start', {'provider_id': 'account'})['ok']
    import json
    data = json.loads(model_profiles_path(agent.home_paths).read_text())
    assert data['providers']['account']['auth']['pending']['device_code'] == 'synthetic-device'


def test_persona_repository_parent_root_cannot_hide_actual_group_target(tmp_path):
    agent, _source, _actor, _raw, _calls = _case(tmp_path)
    repository = PersonaRepository(owner_home=agent.home_paths.root,
        soul_path=agent.home_paths.owner_soul_md, user_path=agent.home_paths.owner_user_md,
        agents_path=agent.home_paths.owner_agents_md)
    with pytest.raises(GroupAuthorizationError):
        repository.mutate(PersonaMutationRequest(target='agents', action='add', content='不能绕过群写门', confirmed=True))


def test_human_group_slash_is_explicit_with_group_wide_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.group_authorization_control import (
        execute_group_authorization_control,
    )
    from agent_py_agent.agent.user_space.group_authorization import read_group_authorization

    agent, source, actor, _raw, _calls = _case(tmp_path)
    monkeypatch.setattr(model_profile_service, '_scoped_model_host', lambda base, scope: (agent, source))
    scope = GatewayControlScope(actor.actor_id, 'feishu', 'oc_source')
    assert execute_group_authorization_control(agent, parse_conversation_control('/group'), scope).ok
    assert read_group_authorization(agent.home_paths)[1]['grants'] == []
    reply = execute_group_authorization_control(agent, parse_conversation_control('/group 监控 全群'), scope)
    assert reply.ok
    block = read_group_authorization(agent.home_paths)[1]
    assert block['grants'][0]['subjects'] == {} and block['grants'][0]['scope'] == 'group'
    stranger = replace(scope, user_id='ou_member')
    denied = execute_group_authorization_control(agent, parse_conversation_control('/group allow monitors.create'), stranger)
    assert not denied.ok and denied.error_code == 'GROUP_AUTHORIZATION_NOT_AUTHORITY'
    result = execute_group_authorization_control(agent,
        parse_conversation_control('/group revoke ' + block['grants'][0]['grant_id']), scope)
    assert result.ok and read_group_authorization(agent.home_paths)[1]['grants'][0]['status'] == 'revoked'


def test_group_model_and_decision_tools_need_no_monitor_authorization(tmp_path):
    from agent_py_agent.agent.capability.model_profile_tool import ManageModelsTool
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

    agent, source, _actor, _raw, _calls = _case(tmp_path)
    tool = ManageModelsTool(agent)
    result = tool.execute({'action': 'add', 'profile': profile()})
    assert result.ok
    current = execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)
    result = UserConfigTool(agent).execute({'action': 'decision_patch', 'changes': {'enabled': True},
        'expected_revision': current['revision']})
    assert result.ok


def test_inherited_empty_group_thread_initialization_needs_no_manual_grant(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.settings.model_profiles import inherited_model_config

    agent, source, _actor, _raw, calls = _case(tmp_path)
    agent.config = AgentConfig(model_backend='echo', my_agent_home=str(agent.home_paths.root))
    assert not source.model_profile_id
    task = SimpleNamespace(attributes={'host_model_profile.v1': {'profile_id': 'default'}}, agent_thread_id=source.thread_id)
    assert inherited_model_config(agent, task).model_backend == 'echo'
    saved = agent.conversation_store.threads.load(source.thread_id)
    assert saved.model_profile_id == 'default' and saved.model_selection_source == 'inherited'
    assert not calls


def test_model_payload_cannot_select_system_settings_source(tmp_path):
    from agent_py_agent.agent.settings.decision_settings_schema import DecisionSettingsUnknownFields

    agent, source, _actor, _raw, _calls = _case(tmp_path)
    current = execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)
    with pytest.raises(DecisionSettingsUnknownFields):
        execute_decision_settings_operation(agent, 'patch', {'changes': {'enabled': True},
            'expected_revision': current['revision'], 'source': 'host_system'}, thread_id=source.thread_id)


@pytest.mark.parametrize('change', ['other_group', 'other_root', 'provider', 'owner_id', 'policy_path'])
def test_approval_configuration_cannot_borrow_host_for_another_target(tmp_path, change):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    agent, _source, actor, raw, _calls = _case(tmp_path)
    home = agent.home_paths
    if change in {'other_group', 'other_root'}:
        root = tmp_path / 'other-root' if change == 'other_root' else home.root
        identity = OwnerIdentity.provider_group('feishu', 'oc_other' if change == 'other_group' else 'oc_source')
        target = home_paths_with_owner(home_paths(root), resolve_owner_home(root, identity))
    else:
        target = replace(home, **{'provider': {'owner_provider': 'other'}, 'owner_id': {'owner_id': 'providers/feishu/groups/oc_other'},
            'policy_path': {'owner_tool_policy_json': home.owner_tool_policy_json.parent / 'other-policy.json'}}[change])
    before = target.owner_tool_policy_json.read_bytes() if target.owner_tool_policy_json.exists() else None
    with pytest.raises(GroupAuthorizationError) as caught:
        execute_approval_mode_operation(target, 'set', 'auto', authorization_host=agent)
    assert caught.value.error_code == 'GROUP_AUTHORIZATION_INVALID'
    after = target.owner_tool_policy_json.read_bytes() if target.owner_tool_policy_json.exists() else None
    assert after == before
