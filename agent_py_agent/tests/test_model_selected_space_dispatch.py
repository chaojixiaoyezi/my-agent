"""模型派活省略空间时使用已受理选择，显式空值仍保持未归属。"""
import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.request_space import REQUEST_SPACE_KEY
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.plugin_host.contracts import HostAPIError
from agent_py_agent.tests.test_bg_w1_business_entry_wiring import (
    _call,
    _chat_thread,
)
from agent_py_agent.tests.test_bg_w1_business_entry_wiring import (
    agent as agent,
)


def prepare(agent):
    thread = _chat_thread(agent, 'selected-space')
    agent._current_run_params = RunParams(request_id='turn-space', task_attributes={'conversation_thread_id': thread.thread_id})
    outcome, response = _call(agent, 'spaces.create', {'name': '当前空间'}, idempotency_key='selected-create')
    assert outcome.ok, outcome.output
    sid = response['data']['space_id']
    agent._current_run_params.host_session_space = {
        'owner_id': agent.home_paths.owner_id, 'actor_id': 'local-agent', 'thread_id': thread.thread_id,
        'space_id': sid, 'revision': 1, 'source': 'thread'}
    return sid


@pytest.mark.parametrize('choice', ['omitted', 'null', 'explicit'])
def test_registered_model_tool_dispatches_exact_selected_or_explicit_space(agent, choice):
    sid = prepare(agent)
    target_space = None if choice == 'null' else sid
    if choice == 'explicit':
        outcome, other = _call(agent, 'spaces.create', {'name': '显式空间'}, idempotency_key='explicit-create')
        assert outcome.ok, outcome.output
        target_space = other['data']['space_id']
    outcome, created = _call(agent, 'background_agents.create', {'name': '执行者', 'space_id': target_space},
        idempotency_key='selected-worker')
    assert outcome.ok, outcome.output
    params = {'agent_id': created['data']['agent']['agent_id'], 'instruction': '检查资料'}
    if choice != 'omitted':
        params['space_id'] = target_space
    outcome, assigned = _call(agent, 'tasks.assign', params, idempotency_key='selected-assign')
    assert outcome.ok, outcome.output
    task = agent.conversation_store.session_tasks.require(assigned['data']['task_id'])
    assert task.metadata['business_context']['space_id'] == target_space
    model_schema = agent.tools.tools['business_tasks_assign'].model_spec.input_schema
    assert 'space_id' not in model_schema['properties']['params']['required']
    assert 'space_id' in business_action_spec('tasks.assign').params_schema['required']


def test_local_model_context_rejects_forged_space_actor(agent):
    prepare(agent)
    agent._current_run_params.host_session_space['actor_id'] = 'someone-else'
    with pytest.raises(HostAPIError) as caught:
        local_business_context(agent, model=True)
    assert caught.value.code == 'CAPABILITY_DENIED'


def test_issued_context_rechecks_frozen_space_on_each_action(agent):
    sid = prepare(agent)
    context, _ = local_business_context(agent, model=True)
    assert context.selected_space_id == sid
    agent._current_run_params.host_session_space['space_id'] = 'changed'
    with pytest.raises(HostAPIError) as caught:
        context.check('tasks.assign', {'owner_id': agent.home_paths.owner_id})
    assert caught.value.code == 'CAPABILITY_DENIED'


def test_same_identity_model_attributes_cannot_grant_selected_space(agent):
    prepare(agent)
    frozen = dict(agent._current_run_params.host_session_space)
    agent._current_run_params.host_session_space = None
    agent._current_run_params.task_attributes[REQUEST_SPACE_KEY] = frozen
    context, _ = local_business_context(agent, model=True)
    assert context.selected_space_id is None


def test_child_attributes_do_not_inherit_host_selection(agent):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    prepare(agent)
    parent = agent._current_run_params
    frozen = dict(parent.host_session_space)
    attrs = create_task_attributes({'attributes': {REQUEST_SPACE_KEY: frozen}}, agent)
    # 模型可写数据仍可作为上下文，但没有宿主专有字段，不能给子代理授予选中空间。
    child = RunParams(request_id='child', task_attributes=attrs)
    assert child.host_session_space is None
    assert replace(parent, attempt_id='retry').host_session_space == frozen
