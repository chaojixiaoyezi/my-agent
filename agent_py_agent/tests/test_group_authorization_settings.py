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


def test_group_model_write_and_explicit_selection_require_grant_but_seed_does_not(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    from agent_py_agent.agent.settings import AgentConfig

    agent.config = AgentConfig(model_backend='echo', my_agent_home=str(agent.home_paths.root))
    assert thread_model_profile_id(agent, source.thread_id) == 'default'
    model_id = str(uuid4())
    payload = {'profile_id': model_id, 'profile': profile()}
    with pytest.raises(GroupAuthorizationError):
        execute_model_profile_operation(agent, 'add', payload, thread_id=source.thread_id)
    assert not model_profiles_path(agent.home_paths).exists()
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
    assert execute_model_profile_operation(agent, 'add', payload, thread_id=source.thread_id)['ok']
    assert thread_model_profile_id(agent, source.thread_id, select=model_id) == model_id
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    with pytest.raises(GroupAuthorizationError):
        thread_model_profile_id(agent, source.thread_id, select='default')
    assert thread_model_profile_id(agent, source.thread_id) == model_id


def test_group_decision_patch_and_reset_both_gate_original_cas(tmp_path):
    agent, source, actor, raw, _calls = _case(tmp_path)
    current = execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)
    payload = {'changes': {'enabled': True}, 'expected_revision': current['revision']}
    with pytest.raises(GroupAuthorizationError):
        execute_decision_settings_operation(agent, 'patch', payload, thread_id=source.thread_id)
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
    saved = execute_decision_settings_operation(agent, 'patch', payload, thread_id=source.thread_id)
    agent._current_run_params.host_channel_actor = replace(actor, actor_id='ou_member')
    with pytest.raises(GroupAuthorizationError):
        execute_decision_settings_operation(agent, 'reset', {'fields': ['enabled'], 'expected_revision': saved['revision']},
            thread_id=source.thread_id)
    assert execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)['revision'] == saved['revision']


def test_group_approval_policy_write_reuses_original_lock_and_preserves_grants(tmp_path):
    agent, _source, actor, raw, _calls = _case(tmp_path)
    assert execute_approval_mode_operation(agent.home_paths, 'get')['ok']
    with pytest.raises(GroupAuthorizationError):
        execute_approval_mode_operation(agent.home_paths, 'set', 'auto', authorization_host=agent)
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
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
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
    with group_capability_guard(agent, 'assistant.configure'):
        assert repository.mutate(request)['entry_id']
    with pytest.raises(GroupAuthorizationError):
        repository.mutate(request)


def test_group_user_config_legacy_yaml_set_and_reset_remain_forbidden(tmp_path):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

    agent, _source, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
    tool = UserConfigTool(agent)
    for action in ('set', 'reset', 'revert'):
        outcome = tool.execute({'action': action, 'key': 'memory_compact_auto_trigger_percent', 'value': 80})
        assert not outcome.ok and outcome.error_code == 'TOOL_PERMISSION_DENIED'


def test_original_signed_group_model_form_rechecks_revocation_but_cancel_is_free(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import model_profile_service
    from agent_py_agent.agent.user_space.group_authorization import (
        GroupAuthorizationChange,
        change_group_authorization,
        read_group_authorization,
    )
    from agent_py_agent.agent.user_space.group_authorization_context import current_group_authority
    from agent_py_agent.tests.test_im_model_cards import callback, setup, submit

    base, _owner, scope, host, _thread = setup(tmp_path, monkeypatch, group=True)
    assert model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope).ok
    assert submit(base, callback(base))['ok']
    block = read_group_authorization(host.home_paths)[1]
    owner_actor = replace(host._current_run_params.host_channel_actor, actor_id='ou_fixture_owner')
    change_group_authorization(host.home_paths, current_group_authority(host), owner_actor,
        GroupAuthorizationChange('revoke', block['revision'], grant_id=block['grants'][0]['grant_id']))
    form = {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
        'model_name': 'synthetic', 'model_context_window_tokens': '64000', 'api_key': 'synthetic-canary'}
    with pytest.raises(GroupAuthorizationError):
        submit(base, callback(base, decision='save', fields=form))
    assert not model_profiles_path(host.home_paths).exists()
    assert submit(base, callback(base, decision='cancel', fields={}))['ok']


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
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
    setup(agent)
    def network(_auth):
        block = read_group_authorization(agent.home_paths)[1]
        change_group_authorization(agent.home_paths, current_group_authority(agent), actor,
            GroupAuthorizationChange('revoke', block['revision'], grant_id=block['grants'][0]['grant_id']))
        return {'device_code': 'synthetic-device', 'user_code': 'ABCD', 'verification_uri': 'https://example.test',
            'interval': 1, 'expires_at': 9999999999}
    monkeypatch.setattr(model_oauth, 'start_device', network)
    with pytest.raises(GroupAuthorizationError):
        execute_model_profile_operation(agent, 'auth_start', {'provider_id': 'account'})
    import json
    data = json.loads(model_profiles_path(agent.home_paths).read_text())
    assert 'device_code' not in data['providers']['account']['auth']['pending']


def test_persona_repository_parent_root_cannot_hide_actual_group_target(tmp_path):
    agent, _source, _actor, _raw, _calls = _case(tmp_path)
    repository = PersonaRepository(owner_home=agent.home_paths.root,
        soul_path=agent.home_paths.owner_soul_md, user_path=agent.home_paths.owner_user_md,
        agents_path=agent.home_paths.owner_agents_md)
    with pytest.raises(GroupAuthorizationError):
        repository.mutate(PersonaMutationRequest(target='agents', action='add', content='不能绕过群写门', confirmed=True))


def test_human_group_slash_is_explicit_and_default_target_is_exact_operator(tmp_path, monkeypatch):
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
    reply = execute_group_authorization_control(agent, parse_conversation_control('/group allow assistant.configure'), scope)
    assert reply.ok
    block = read_group_authorization(agent.home_paths)[1]
    assert block['grants'][0]['subjects'] == {actor.actor_id: 0}
    stranger = replace(scope, user_id='ou_member')
    denied = execute_group_authorization_control(agent, parse_conversation_control('/group allow monitors.create'), stranger)
    assert not denied.ok and denied.error_code == 'GROUP_AUTHORIZATION_NOT_AUTHORITY'
    result = execute_group_authorization_control(agent,
        parse_conversation_control('/group revoke ' + block['grants'][0]['grant_id']), scope)
    assert result.ok and read_group_authorization(agent.home_paths)[1]['grants'][0]['status'] == 'revoked'


def test_group_model_and_decision_tool_rejections_keep_stable_permission_code(tmp_path):
    from agent_py_agent.agent.capability.model_profile_tool import ManageModelsTool
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

    agent, source, _actor, _raw, _calls = _case(tmp_path)
    tool = ManageModelsTool(agent)
    result = tool.execute({'action': 'add', 'profile': profile()})
    assert not result.ok and result.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    current = execute_decision_settings_operation(agent, 'read', {}, thread_id=source.thread_id)
    result = UserConfigTool(agent).execute({'action': 'decision_patch', 'changes': {'enabled': True},
        'expected_revision': current['revision']})
    assert not result.ok and result.error_code == 'GROUP_AUTHORIZATION_REQUIRED'


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
    _allow(agent, raw, actor, capability='assistant.configure', subject=actor.actor_id)
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
