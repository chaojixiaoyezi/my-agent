"""partial交付观察沿原结果metadata到真实HTTP，不升级为产物或授权。"""
from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_execution import (
    collect_background_execution_result,
)
from agent_py_agent.agent.conversation.session_task_report import TaskTurnOutcome, close_out_turn
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401
    assigned_task,
    call,
    native,
)


def test_partial_delivery_diagnostic_reaches_original_task_public_coverage(native):  # noqa: F811
    _, host = native
    _, task_id = assigned_task(native, name='partial-delivery')
    task = host.conversation_store.session_tasks.require(task_id)
    turn = 'partial-' + task_id
    host.conversation_store.session_tasks.bind_turn(task_id, turn_id=turn)
    host.conversation_store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': task_id, 'goal': '交付观察'})
    host.conversation_store.tasks.update_status({'task_id': task_id, 'status': 'completed'})
    diagnostic = {'error_code': 'CHILD_DELIVERY_REGISTRY_BUDGET_EXCEEDED', 'coverage': 'partial',
        'exception_type': 'PermissionError', 'run_id': '/owners/foreign/private', 'next_action': '/private/secret', 'path': '/private/secret'}
    result = SimpleNamespace(archive_tool_calls=[], delivery_artifact_diagnostics=[diagnostic],
        delivery_artifacts=[], response='原工作完成，但交付登记尚待核验。', runtime_status='ok')
    execution = collect_background_execution_result(host, result, {})
    finished = close_out_turn(host, turn, TaskTurnOutcome(ok=True, summary=execution.response,
        delivery_artifact_diagnostics=execution.delivery_artifact_diagnostics))
    assert finished is not None and finished.result_refs == ()
    status, response = call(native, 'tasks.get', {'task_id': task_id})
    assert status == 200, response
    coverage = response['data']['result_ref_coverage']
    assert coverage['state'] == 'partial' and response['data']['result_refs'] == []
    row = coverage['diagnostics'][0]
    assert row['error_code'] == diagnostic['error_code']
    assert row['stage'] == 'delivery_observation' and row['effect_outcome'] == 'not_started'
    assert row['source_effect_outcome'] == 'unknown' and row['exception_type'] == 'PermissionError'
    assert 'tasks.get' in row['next_action'] and 'artifacts.get' in row['next_action']
    assert '/private' not in str(response) and '/owners/foreign' not in str(response)
    assert 'run_id' not in row and 'path' not in row
