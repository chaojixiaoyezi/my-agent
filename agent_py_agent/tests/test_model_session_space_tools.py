"""自然语言模型沿真实注册工具切换当前会话，子代理不获得选择权。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.gateway_parts.business_api import _local_business_services
from agent_py_agent.tests.test_bg_w1_business_entry_wiring import (
    _call,
    _chat_thread,
)
from agent_py_agent.tests.test_bg_w1_business_entry_wiring import (
    agent as agent,
)


def foreground(agent):
    thread = _chat_thread(agent, 'model-space-switch')
    agent._current_run_params = RunParams(request_id='foreground', source='gateway',
        task_attributes={'conversation_thread_id': thread.thread_id})
    return thread


def test_registered_model_tools_switch_same_session_and_recover_unreadable_selection(agent, monkeypatch):
    thread = foreground(agent)
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    discovered = execute_registry_test_call(agent.tools, 'tool_search', {'query': '切换空间', 'limit': 5})
    assert discovered.ok, discovered.output
    assert 'business_sessions_space_set' in discovered.metadata['handler_details']['tool_search']['loaded_tool_names']
    spaces = []
    for name in ('first', 'second'):
        outcome, created = _call(agent, 'spaces.create', {'name': name}, idempotency_key='create-' + name)
        assert outcome.ok, outcome.output
        spaces.append(created['data']['space_id'])
    outcome, first = _call(agent, 'sessions.space.set', {'space_id': spaces[0]}, expected_revision=0,
        idempotency_key='set-first')
    assert outcome.ok and first['data']['revision'] == 1, outcome.output
    from agent_py_agent.agent.conversation.business_access import BusinessError

    service = _local_business_services(agent).spaces
    original_authorize = service.authorize_call

    def authorize(action, params, **kwargs):
        if params.get('space_id') == spaces[0]:
            raise BusinessError('SPACE_NOT_FOUND')
        return original_authorize(action, params, **kwargs)

    monkeypatch.setattr(service, 'authorize_call', authorize)
    # 选中指针不依赖昂贵的详情/挂载投影；旧空间不可用也能先读CAS版本再切换。
    monkeypatch.setattr(_local_business_services(agent).spaces, 'get',
        lambda *a, **kw: pytest.fail('选择操作不能读取空间详情'))
    outcome, selected = _call(agent, 'sessions.space.get')
    assert outcome.ok and selected['data']['space_id'] == spaces[0], outcome.output
    outcome, changed = _call(agent, 'sessions.space.set', {'space_id': spaces[1]}, expected_revision=1,
        idempotency_key='set-second')
    assert outcome.ok and changed['data']['revision'] == 2, outcome.output
    outcome, replay = _call(agent, 'sessions.space.set', {'space_id': spaces[1]}, expected_revision=1,
        idempotency_key='set-second')
    assert outcome.ok and replay['data'] == changed['data'], outcome.output
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['session_space.v1']


@pytest.mark.parametrize('source', ['subagent_run_model_turn', 'run'])
def test_child_and_unbound_host_cannot_change_session_selection(agent, source):
    foreground(agent)
    agent._current_run_params = replace(agent._current_run_params, source=source)
    outcome, result = _call(agent, 'sessions.space.set', {'space_id': 'untrusted'}, expected_revision=0,
        idempotency_key='denied-selection')
    assert not outcome.ok and result['error']['code'] == 'CAPABILITY_DENIED'


def test_forged_actor_is_not_a_session_selection_source(agent):
    foreground(agent)
    agent._current_run_params.host_channel_actor = {'owner_id': agent.home_paths.owner_id}
    outcome, result = _call(agent, 'sessions.space.get')
    assert not outcome.ok and result['error']['code'] == 'CHANNEL_ACTOR_IDENTITY_INVALID'
    assert '身份记录' in result['error']['message'] and '原会话' in result['error']['message']
    assert result['effect_outcome'] == 'not_started'


def test_session_space_rejects_issued_context_without_model_source(agent):
    from agent_py_agent.agent.gateway_parts.business_api import (
        bind_business_context,
        execute_business,
        local_business_context,
    )

    foreground(agent)
    real, services = local_business_context(agent, model=True)
    ordinary, _ = local_business_context(agent, model=False)
    forged_actor = bind_business_context(agent, authorize=lambda *_: True, actor=real.actor)
    request = {'action': 'sessions.space.get', 'request_id': 'source-check', 'params': {}}
    for missing in (ordinary, forged_actor):
        status, payload = execute_business(request, missing, services)
        assert status == 403
        assert payload['error']['code'] == 'SESSION_SPACE_SOURCE_REQUIRED'
        assert payload['effect_outcome'] == 'not_started'
    status, payload = execute_business(request, real, services)
    assert status == 200 and payload['data']['space_id'] is None


def test_registered_model_tool_clears_same_actor_without_reauthorizing_old_space(agent, monkeypatch):
    foreground(agent)
    outcome, created = _call(agent, 'spaces.create', {'name': 'clear-model-space'}, idempotency_key='clear-create')
    assert outcome.ok, outcome.output
    space = created['data']['space_id']
    outcome, _ = _call(agent, 'sessions.space.set', {'space_id': space}, expected_revision=0,
        idempotency_key='clear-model-select')
    assert outcome.ok, outcome.output
    service = _local_business_services(agent).spaces
    monkeypatch.setattr(service, 'authorize_call', lambda *_args, **_kwargs:
        pytest.fail('清除自己的选择不能依赖旧空间仍可读取'))
    outcome, cleared = _call(agent, 'sessions.space.set', {'space_id': None}, expected_revision=1,
        idempotency_key='clear-model-reset')
    assert outcome.ok and cleared['data'] == {'space_id': None, 'revision': 2, 'source': 'default'}, outcome.output
    outcome, replay = _call(agent, 'sessions.space.set', {'space_id': None}, expected_revision=1,
        idempotency_key='clear-model-reset')
    assert outcome.ok and replay['data'] == cleared['data'], outcome.output
    outcome, read = _call(agent, 'sessions.space.get')
    assert outcome.ok and read['data'] == cleared['data'], outcome.output


@pytest.mark.parametrize('source', ['subagent_run_model_turn', 'run'])
def test_child_and_unbound_host_cannot_clear_session_selection(agent, source):
    foreground(agent)
    agent._current_run_params = replace(agent._current_run_params, source=source)
    outcome, result = _call(agent, 'sessions.space.set', {'space_id': None}, expected_revision=0,
        idempotency_key='denied-clear')
    assert not outcome.ok and result['error']['code'] == 'CAPABILITY_DENIED'


def test_native_business_error_keeps_control_separate_from_host_display(agent):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    foreground(agent)
    first, _ = _call(agent, 'spaces.create', {'name': 'host-error-display'},
                     idempotency_key='host-error-first')
    assert first.ok
    outcome, result = _call(agent, 'spaces.create', {'name': 'HOST-ERROR-DISPLAY'},
                            idempotency_key='host-error-conflict')
    assert not outcome.ok and result['error']['code'] == 'SPACE_NAME_CONFLICT'
    assert result['effect_outcome'] == 'not_started'
    assert outcome.error_code == error_contract('SPACE_NAME_CONFLICT').code
    host = outcome.metadata['host_error']
    assert host['http_status'] == 409 and host['code'] == 'SPACE_NAME_CONFLICT'
    assert host['source'] == 'business_host' and host['next_action']
    header = outcome.render_for_prompt().splitlines()[0]
    assert '; error_code=SPACE_NAME_CONFLICT;' in header
    assert f'control_error_code={outcome.error_code}' in header and 'http_status=409' in header
    assert '[host-business-recovery]' in outcome.render_for_prompt()
    assert not outcome.retryable
