"""业务授权装配在handler之前拒绝，不伪造未知写入或扩大source权限。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_runtime import _business_scope_preflight
from agent_py_agent.agent.conversation import business_context
from agent_py_agent.agent.plugin_host.errors import HostAPIError


def test_explicit_host_authorization_denial_is_not_started(monkeypatch):
    def denied(host):
        raise HostAPIError('CAPABILITY_DENIED', 403, message='BUSINESS_ACTOR_SOURCE_MISSING：原来源未绑定')

    monkeypatch.setattr(business_context, 'business_execution_policy_scope', denied)
    scope, result = _business_scope_preflight(SimpleNamespace(agent=object(), call=SimpleNamespace(tool_name='send_session_message')))
    assert scope == {} and not result.ok
    assert result.error_code == result.reported_error_code == 'CAPABILITY_DENIED'
    assert result.effect_outcome == 'not_started' and not result.handler_executed
    assert result.failure_stage == 'authorization'
    assert 'BUSINESS_ACTOR_SOURCE_MISSING' in result.output


@pytest.mark.parametrize('effect', ['unknown', 'committed'])
def test_unconfirmed_host_effect_is_never_downgraded_to_not_started(monkeypatch, effect):
    def unknown(host):
        raise HostAPIError('OUTCOME_UNKNOWN', 503, effect)

    monkeypatch.setattr(business_context, 'business_execution_policy_scope', unknown)
    with pytest.raises(HostAPIError) as raised:
        _business_scope_preflight(SimpleNamespace(agent=object(), call=SimpleNamespace(tool_name='write_file')))
    assert raised.value.outcome == effect


def test_unexpected_source_exception_keeps_original_unknown_path(monkeypatch):
    def broken(host):
        raise OSError('synthetic unreadable source')

    monkeypatch.setattr(business_context, 'business_execution_policy_scope', broken)
    with pytest.raises(OSError):
        _business_scope_preflight(SimpleNamespace(agent=object(), call=SimpleNamespace(tool_name='write_file')))


from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
from agent_py_agent.tests import test_business_model_tools as model_tools
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call
from agent_py_agent.tests.test_capability_package_runtime_binding import _unpromoted_turn

agent = model_tools.agent


def test_original_registry_preflight_rejection_never_enters_write_handler(agent, tmp_path, monkeypatch):
    thread, params = _unpromoted_turn(agent)
    agent._current_run_params = params
    agent.tools.operation_store = None
    agent.tools.operation_store_required = False
    target = agent.effective_workspace_root / 'must-not-write.txt'
    call = canonical_test_call(params.tool_runtime_snapshot, 'write_file',
        {'path': str(target), 'content': 'must not exist'}, call_id='source-denied-write')

    def denied(host):
        raise HostAPIError('CAPABILITY_DENIED', 403, message='BUSINESS_ACTOR_SOURCE_MISSING：缺原来源')

    monkeypatch.setattr(business_context, 'business_execution_policy_scope', denied)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    result = execution.result
    assert not result.ok and not target.exists()
    assert result.error_code == result.reported_error_code == 'CAPABILITY_DENIED'
    assert not result.handler_executed and result.effect_outcome == 'not_started'
    assert result.failure_stage == 'authorization'
