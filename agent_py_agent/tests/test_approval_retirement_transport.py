"""审批纯退休运输只保同一个原异常，不授新scope、不让普通同码异常获得未结身份。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner.session_pool import _raise_transport_unknown
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalWaitOptions,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.runtime_db.host_command_execution import execute_host_command
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_approval_waiter_retirement import held_original_lock
from agent_py_agent.tests.test_host_command_approval import approval_case
from agent_py_agent.tests.test_tool_approval_foreground_scope import _foreground


# LLM: 真实临时owner/RuntimeDB/main claim发布原审批，再以另一Thread合法持原锁得native异常；不直接造type或写私有row。
# 函数用途: 为运输边界准备一个真实原退休错误，仅用于同对象上抛，不能当被测宿主命令的授权来源。
def original_retirement_error(tmp_path):
    world = _foreground(tmp_path, kind='user')
    request = build_tool_approval_request(ToolCall(call_id='native-retirement-transport', tool_name='controlled_exec',
        arguments={'apply': True}, source_protocol='native', schema_hash='sha256:retirement-transport',
        run_id='durable-task', turn_id='original-transport-turn', attempt_id=world.params.attempt_id),
        request_id='message', round_number=1, call_index=1, description='合成原未结运输')
    handle = approval.publish_agent_tool_approval(world.agent, run_id='durable-task',
        thread_id=world.thread.thread_id, request_value=request)
    with held_original_lock(approval._transition_path(handle.path)):
        with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
            approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
    return caught.value


@pytest.mark.parametrize('actual', [True, False])
def test_host_command_outer_catch_preserves_original_retirement_without_handler(tmp_path, actual):
    error = original_retirement_error(tmp_path / 'approval') if actual else RuntimeError('APPROVAL_RETIREMENT_UNCONFIRMED')
    repo, request, tool, prepare = approval_case(tmp_path / 'command')

    def consumer(*_args, **_kwargs):
        raise error

    if actual:
        with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
            execute_host_command(repo, request, prepare, request_permission=consumer)
        assert caught.value is error
        assert tool.calls == 0
        assert approval.retry_approval_retirement(error).status == 'no_input'
    else:
        result = execute_host_command(repo, request, prepare, request_permission=consumer)
        assert result['state'] == 'rejected' and result['error_code'] == 'APPROVAL_UNAVAILABLE'
        assert tool.calls == 0


def test_child_outer_helper_preserves_native_and_ignores_plain_code(tmp_path):
    error = original_retirement_error(tmp_path)
    with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
        _raise_transport_unknown(error)
    assert caught.value is error
    assert _raise_transport_unknown(RuntimeError('APPROVAL_RETIREMENT_UNCONFIRMED')) is None
    # 裸原wait没有Gateway原producer；清理可完成，但不能冒称canonical已接收请求责任。
    assert approval.retry_approval_retirement(error).status == 'no_input'
    assert error._retirement_origin.observation is None


def test_sink_actual_native_retirement_is_not_mapped_to_unavailable(tmp_path, monkeypatch):
    error = original_retirement_error(tmp_path)
    world = _foreground(tmp_path / 'sink', kind='user')
    sink = approval.AgentToolApprovalSinkMixin()
    sink.agent, sink.thread_id, sink.task_id = world.agent, world.thread.thread_id, 'durable-task'
    sink.approval_interactive = True
    request = error.obligation.request
    monkeypatch.setattr(approval, 'publish_agent_tool_approval', lambda *_a, **_k: object())

    def original_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(approval, 'wait_for_agent_tool_approval', original_error)
    # 仅锁定原sink错误运输，不从此consumer替身证明新run的批准资格。
    from agent_py_agent.agent.user_space import approval_mode
    monkeypatch.setattr(approval_mode, 'autonomous_tool_decision', lambda *_a: None)
    with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
        sink.request_permission(request.to_dict())
    assert caught.value is error
