"""V03-06 回归：usage 投影不得把「选中过」的操作用作执行证据。

原实现把 runtime_db 里每个 tool_operation 都标成 phase=executed，连
CLAIMED 且 handler 未启动、CANCELLED、UNKNOWN 也算执行过，
等于拿计划/取消记录冒充执行收据。

修复点：agent/capability/business_capabilities.py 的 usage_phase_for_operation。
"""
import pytest

from agent_py_agent.agent.capability.business_capabilities import usage_phase_for_operation


@pytest.mark.parametrize('status', ['CLAIMED', 'CANCELLED'])
def test_not_started_operations_are_not_executed(status):
    phase, reasons = usage_phase_for_operation(status, 0)
    assert phase == 'selected'
    assert reasons == ['not_executed']


def test_claimed_but_handler_started_counts_as_executed():
    phase, reasons = usage_phase_for_operation('CLAIMED', 1794534000.0)
    assert phase == 'executed'
    assert reasons == ['runtime_execution_receipt']


def test_unknown_outcome_is_selected_not_executed():
    phase, reasons = usage_phase_for_operation('UNKNOWN', 1794534000.0)
    assert phase == 'selected'
    assert reasons == ['execution_unknown']


@pytest.mark.parametrize('status', ['EXECUTING', 'SUCCEEDED', 'FAILED'])
def test_real_execution_states_are_executed(status):
    phase, reasons = usage_phase_for_operation(status, 1794534000.0)
    assert phase == 'executed'
    assert reasons == ['runtime_execution_receipt']


def test_unparsable_handler_start_is_treated_as_not_started():
    phase, reasons = usage_phase_for_operation('CLAIMED', 'not-a-timestamp')
    assert phase == 'selected'
    assert reasons == ['not_executed']
