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
