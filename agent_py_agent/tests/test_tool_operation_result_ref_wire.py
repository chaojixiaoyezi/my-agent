"""操作复合URI不是单个ID；原producer与Client/Server保持完整ref。"""
from dataclasses import replace

from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.tool_operation_coordinator import execute_tool_operation
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
    original,
    produced_operation,
)

_FIXTURES = (background_http, isolated_home)


def test_native_long_operation_result_ref_survives_list_and_detail(background_http):  # noqa: F811
    services, task = original.observed_task(background_http)
    _, _, _, _, request = produced_operation(services, task)
    operation_id = 'workflow-review-dispatch-' + 'a' * 64
    long_request = replace(request, operation_id=operation_id, idempotency_key=operation_id,
        invoke=lambda: ToolHandlerOutcome('background_agents', True, '{}', call_id=operation_id,
            handler_executed=True, duration_ms=1))
    result = execute_tool_operation(long_request)
    assert result.ok
    expected = 'tool-operation://' + request.run_id + '/' + operation_id
    assert 128 < len(expected) < 512
    client = client_for(background_http)
    listed = client.call('tool_calls.list', {'filter': {'run_id': request.run_id}, 'limit': 30})
    assert listed['ok'], listed
    row = next(item for item in listed['data']['items'] if item['operation_id'] == operation_id)
    assert row['result_ref'] == expected
    detail = client.call('tool_calls.get', {'operation_id': operation_id})
    assert detail['ok'], detail
    assert detail['data']['result_ref'] == expected
    assert detail['data']['tool_call']['result_ref'] == expected
