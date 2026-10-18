"""真实Agent.run先恢复Params再外抛，退休未结不能提前写ordinary failed运行账。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_approval_waiter_retirement import held_original_lock
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


@pytest.mark.parametrize("actual", [True, False])
def test_real_agent_run_restores_params_and_retains_unconfirmed_original_attempt(tmp_path, monkeypatch, actual):
    world = _foreground(tmp_path, kind='user')
    agent = world.agent
    prior_params = agent._current_run_params
    seen = {}

    def fake_loop(actual_agent, loop_params):
        assert actual_agent is agent
        params = agent._current_run_params
        assert type(params) is runtime_mixin.RunParams and params is not prior_params and params.attempt_id
        seen['params'] = params
        if not actual:
            seen['error'] = RuntimeError('APPROVAL_RETIREMENT_UNCONFIRMED')
            raise seen['error']
        request = build_tool_approval_request(ToolCall(call_id='real-agent-retirement', tool_name='controlled_exec',
            arguments={'apply': True}, source_protocol='native', schema_hash='sha256:real-agent-retirement',
            run_id=params.task_id, turn_id='actual-agent-turn', attempt_id=params.attempt_id),
            request_id=params.request_id, round_number=1, call_index=1, description='合成审批退休')
        handle = approval.publish_agent_tool_approval(agent, run_id=params.task_id,
            thread_id=world.thread.thread_id, request_value=request)
        with held_original_lock(approval._transition_path(handle.path)):
            try:
                approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
            except approval.ApprovalRetirementUnconfirmedError as error:
                seen['error'] = error
                raise

    monkeypatch.setattr(runtime_mixin, '_execute_runtime_loop', fake_loop)
    with pytest.raises(approval.ApprovalRetirementUnconfirmedError if actual else RuntimeError) as caught:
        agent.run('合成普通取消', params=replace(world.params, run_id='actual-agent-retirement-run',
            attempt_id='', save=False))
    assert caught.value is seen['error']
    assert agent._current_run_params is prior_params
    repo = agent.subagents.runtime_db
    attempt = repo.get_attempt(seen['params'].attempt_id)
    assert attempt['status'] == ('running' if actual else 'failed')
    assert bool(attempt['ended_at']) is not actual
    run = repo.agent_run_for_run_id(seen['params'].run_id)
    assert (run['status'] != 'failed') is actual
    # 原exec资源锁按活PID保守持有；这证明B未完成后续会话恢复，不能手清锁冒充已解卡。
    assert repo.has_active_exec_lock(run['agent_run_id']) is actual
