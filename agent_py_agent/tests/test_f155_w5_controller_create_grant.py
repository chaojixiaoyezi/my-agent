"""总控事实只从持久线程取得，默认目录只增加一个业务动作。"""
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_context import tool_policy_request
from agent_py_agent.agent.conversation.background_tool_policy import (
    BackgroundToolPolicyRequest,
    background_allowed_tools,
)
from agent_py_agent.agent.conversation.models import SESSION_TASK_WAKE_REASON
from agent_py_agent.tests.test_business_v12_f7_observation import fixture


def test_controller_fact_and_exact_action_delta(tmp_path):
    case = fixture(tmp_path)
    agent = SimpleNamespace(conversation_store=case.store, config=None)
    request = SimpleNamespace(thread_id='controller', reason=SESSION_TASK_WAKE_REASON, wake_signal={})
    policy = tool_policy_request(agent, request)
    assert policy.business_controller is True
    ordinary = background_allowed_tools(request=BackgroundToolPolicyRequest(reason=SESSION_TASK_WAKE_REASON))
    controller = background_allowed_tools(request=policy)
    assert set(controller) - set(ordinary) == {'business_execution:background_agents.create'}
    request.thread_id = 'background'
    request.wake_signal = {'metadata': {'business_controller': True}}
    assert tool_policy_request(agent, request).business_controller is False


def test_configured_replacement_and_scheduled_profile_do_not_gain_create():
    config = SimpleNamespace(background_main_agent_allowed_tools=['read_file'])
    assert background_allowed_tools(request=BackgroundToolPolicyRequest(
        reason=SESSION_TASK_WAKE_REASON, business_controller=True, config=config)) == ['read_file']
    assert 'business_execution:background_agents.create' not in background_allowed_tools(
        request=BackgroundToolPolicyRequest(reason='scheduled_job_due', business_controller=True))


def test_create_original_http_action_and_idempotency(background_http):
    from agent_py_agent.tests.test_mega_business_api_actions import (
        background_call,
        create_background,
    )

    params, agent = create_background(background_http)
    status, replay = background_call(background_http, 'background_agents.create', params,
        idempotency_key='wire-background')
    assert status == 200 and replay['effect_outcome'] == 'committed'
    assert replay['data']['agent'] == agent
    status, denied = background_call(background_http, 'background_agents.create', {
        'space_id': params['space_id'], 'name': '其它Owner'}, user='business-bob', idempotency_key='no-grant')
    assert status == 404 and denied['effect_outcome'] == 'not_started'


from agent_py_agent.tests.test_mega_business_api_actions import background_http  # noqa: E402

_FIXTURES = (background_http,)


def test_real_wake_run_params_preserve_canonical_controller_grant(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _background_preload_tool_names
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        GoalRuntimeContext,
        _run_params,
    )
    from agent_py_agent.agent.settings import AgentConfig

    case = fixture(tmp_path)
    agent = SimpleNamespace(conversation_store=case.store, config=AgentConfig(enable_tools=False),
        home_paths=SimpleNamespace(owner_id=case.owner))
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    task = case.store.session_tasks.create(SessionTaskDraft('background', 'controller', '真实派活',
        dedupe_key='real-controller-wake'))
    wake = case.store.wakes.raise_signal({'thread_id': 'controller', 'reason': SESSION_TASK_WAKE_REASON,
        'metadata': {'session_task_id': task.task_id}})
    request = BackgroundRunRequest(thread_id='controller', reason=SESSION_TASK_WAKE_REASON,
        wake_signal=wake.to_dict())
    params = _run_params('controller', request, agent, goal_context=GoalRuntimeContext())
    assert params.run_id == params.request_id == task.task_id
    assert 'business_execution:background_agents.create' in params.allowed_tools
    assert 'business_execution' in _background_preload_tool_names(params, params.task_attributes or {})
    ordinary = _run_params('background', BackgroundRunRequest(thread_id='background',
        reason=SESSION_TASK_WAKE_REASON, wake_signal={'metadata': {'business_controller': True}}),
        agent, goal_context=GoalRuntimeContext())
    assert 'business_execution:background_agents.create' not in ordinary.allowed_tools
    assert set(params.allowed_tools) - set(ordinary.allowed_tools) == {'business_execution:background_agents.create'}


def test_preload_cannot_mint_action_from_attrs(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _background_preload_tool_names
    from agent_py_agent.agent.conversation import background_tool_policy

    requests = []
    original = background_tool_policy.background_initially_presented_tools
    def observe(request):
        requests.append(request)
        return original(request)
    monkeypatch.setattr(background_tool_policy, 'background_initially_presented_tools', observe)
    params = SimpleNamespace(allowed_tools=['business_execution', 'business_execution:tasks.cancel'])
    _background_preload_tool_names(params, {'business_controller': True,
        'wake_signal': {'reason': SESSION_TASK_WAKE_REASON}})
    assert requests[0].business_controller is False
    assert requests[0].allowed_tools == tuple(params.allowed_tools)
    params.allowed_tools.append('business_execution:background_agents.create')
    _background_preload_tool_names(params, {'wake_signal': {'reason': SESSION_TASK_WAKE_REASON}})
    assert requests[1].business_controller is True
    assert requests[1].allowed_tools == tuple(params.allowed_tools)
