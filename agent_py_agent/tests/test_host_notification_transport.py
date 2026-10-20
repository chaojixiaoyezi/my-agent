"""原C/W短门内通知新attempt的运输绑定；无模型/网络/真实进程。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.host_notification_scope import notification_scope
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
from agent_py_agent.tests.test_background_claim_transport_reference import actual as actual
from agent_py_agent.tests.test_background_claim_transport_reference import c_actual as c_actual
from agent_py_agent.tests.test_background_claim_transport_reference import case as case
from agent_py_agent.tests.test_background_claim_transport_reference import (
    claimed,
    claims,
    gateway_scope,
    install_core,
    observe_claim_writes,
)
from agent_py_agent.tests.test_background_claim_transport_reference import context as context
from agent_py_agent.tests.test_background_claim_transport_reference import (
    transport_case as transport_case,
)


# LLM: 原C/W/P3/P5及Store/RuntimeDB都保留，只将业务参数准备缩成明确本地root；不替换BG binder、core或publisher。
# 函数用途: 在既有真实运输夹具上准备一个活动TaskLink，不运行模型或业务工具。
def prepare_notification(transport_case, tmp_path, monkeypatch):
    kind, case = transport_case
    _source, _claim, agent, store, thread, *_ = case
    install_core(case, tmp_path, monkeypatch)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'ports-task',
        'goal': '合成通知阶段', 'status': 'active'})
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation import runtime
    from agent_py_agent.agent.settings import AgentConfig

    agent.config = AgentConfig(my_agent_home=str(agent.home_paths.root),
        my_agent_owner_provider=agent.home_paths.owner_provider,
        my_agent_owner_kind=agent.home_paths.owner_kind, my_agent_owner_id=agent.home_paths.owner_id.rsplit("/", 1)[-1])
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig())
    def params(*_args, **_kwargs):
        return RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task',
            task_attributes={'conversation_thread_id': thread.thread_id, 'conversation_task_id': 'ports-task'})
    monkeypatch.setattr(runtime, '_run_params', params)
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id='ports-task', reason='synthetic')
    return kind, case, request


def test_notification_retains_original_mp_previous_and_same_t_publication(transport_case, tmp_path, monkeypatch):
    kind, case, request = prepare_notification(transport_case, tmp_path, monkeypatch)
    _source, claim, agent, store, thread, *_ = case
    writes = observe_claim_writes(monkeypatch)
    def run_once(_kwargs):
        with notification_scope(agent, request) as turn:
            previous = turn.params.conversation_task_binding_callback._previous
            assert type(previous) is bridge.BackgroundRuntimeBinding
            assert previous.scope is bridge.current_background_request()
            assert previous.invocation_id == turn.params.request_id == 'ports-turn'
        return 'notification-complete'
    deps, args, row, _policy = claimed(case, run_once)
    with gateway_scope(kind, case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'notification-complete'
    assert len(writes) == 1
    ref = store.claims.load(thread.thread_id)['gateway_transport_reference']
    assert ref['request_id'] == claim.request_id and ref['invocation_run_id'] == 'ports-turn'
    assert ref['claim_id'] == row['claim_id']
    assert json.loads(claim.path.read_text())['runtime_authority']['attempt_id'] == ref['core_attempt_id']


@pytest.mark.parametrize('stage', ['before_bind', 'body'])
def test_lost_mp_source_keeps_original_unknown_claim(transport_case, tmp_path, monkeypatch, stage):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )

    kind, case, request = prepare_notification(transport_case, tmp_path, monkeypatch)
    _source, claim, agent, store, thread, *_ = case
    errors = []
    original_error = bridge._uncertain_execution
    def observe_error(*args, **kwargs):
        result = original_error(*args, **kwargs)
        errors.append(result)
        return result
    monkeypatch.setattr(bridge, '_uncertain_execution', observe_error)
    def run_once(_kwargs):
        if stage == 'before_bind':
            bridge.current_background_request().lifetime.active = False
        with notification_scope(agent, request):
            bridge.current_background_request().lifetime.active = False
            bridge.background_short_operation('notification-final-send', lambda: pytest.fail('lost source reached effect'))
    deps, args, row, policy = claimed(case, run_once)
    with gateway_scope(kind, case):
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
    assert errors and caught.value is errors[0]
    assert caught.value.request_id == claim.request_id and caught.value.operation_entered is False
    assert store.claims.load(thread.thread_id)['status'] == 'running'
    assert not policy and claim.path.exists()
