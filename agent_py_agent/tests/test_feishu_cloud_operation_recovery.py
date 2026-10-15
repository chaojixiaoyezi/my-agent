"""真实Registry/原操作账核对云写入，只替换本地供应商运输。"""
import json

import pytest

from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import (
    _authority_attempt_id,
    execute_registry_test_call,
)
from agent_py_agent.tests.test_feishu_cloud_tool import _agent, _call, _grant, cloud_server
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active

_FIXTURES = (cloud_server,)


# LLM: 原Registry和managed操作账保留准确run/task/operation；旧调用编号重复执行只进入原核对claim。
# 函数用途: 在真实活动任务中执行同一云操作，避免用新call编号绕过UNKNOWN。
def _execute(agent, action, *, call_id='original-call', refresh_attempt=False, operation_id='cloud-original-operation'):
    current = agent._current_run_params
    if refresh_attempt or not getattr(agent, '_cloud_recovery_test_attempt', ''):
        agent._cloud_recovery_test_attempt = _authority_attempt_id(agent, current.run_id, task_id=current.task_id)
    return execute_registry_test_call(agent.tools, 'feishu_cloud',
        {'action':action, 'resource_token':'sheet_one', 'range':'sid!A1:B1', 'values':[['合成分析结论',7]]},
        run_id=current.run_id, call_id=call_id, operation_id=operation_id,
        attempt_id=agent._cloud_recovery_test_attempt,
        write_boundary={'task_id':current.task_id}, register_with=agent)


@pytest.mark.parametrize('target_matches', [True, False])
@pytest.mark.parametrize('new_attempt', [False, True])
def test_unknown_update_only_reads_original_target_and_preserves_analysis(tmp_path, cloud_server, target_matches, new_attempt):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    _active(agent)
    assert _call(agent, 'sheet_read', range='sid!A1:B1').ok
    analysis = agent.root/'completed_analysis.txt'
    analysis.write_text('合成已完成分析：不应重做。')
    cloud_server.fail_http = True
    first = _execute(agent, 'sheet_update')
    assert not first.ok and first.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert first.effect_outcome == 'unknown'
    cloud_server.fail_http = False
    cloud_server.response_data_override = {'valueRange':{'range':'sid!A1:B1',
        'values':[['合成分析结论',7]] if target_matches else [['不同状态',0]]}}
    result = _execute(agent, 'sheet_update', refresh_attempt=new_attempt)
    assert sum(call[0] == 'PUT' for call in cloud_server.calls) == 1
    assert analysis.read_text() == '合成已完成分析：不应重做。'
    if target_matches:
        assert result.ok, (result.error_code, result.output)
        assert '合成分析结论' not in result.output
        assert result.operation.status == 'succeeded' and not result.handler_executed
    else:
        assert not result.ok and result.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
        assert not result.handler_executed


def test_unknown_append_has_no_guessed_position_or_repeat_write(tmp_path, cloud_server):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    _active(agent)
    assert _call(agent, 'sheet_read', range='sid!A1:B1').ok
    cloud_server.fail_http = True
    first = _execute(agent, 'sheet_append')
    assert not first.ok and first.effect_outcome == 'unknown'
    calls = len(cloud_server.calls)
    cloud_server.fail_http = False
    result = _execute(agent, 'sheet_append', call_id='attempt-to-change-call-id', operation_id='new-call-operation', refresh_attempt=True)
    assert not result.ok and result.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert len(cloud_server.calls) == calls
    assert len(cloud_server.rows) == 1


@pytest.mark.parametrize('provider_code,expected', [(90202,'FEISHU_CLOUD_RANGE_INVALID'), (90215,'FEISHU_CLOUD_SHEET_NOT_FOUND')])
def test_explicit_provider_rejection_is_failed_not_unknown_in_registry(tmp_path, cloud_server, provider_code, expected):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    assert _call(agent, 'sheet_read', range='sid!A1:B1').ok
    cloud_server.business_error_code = provider_code
    result = _call(agent, 'sheet_append', range='sid!A1:B1', values=[['synthetic',1]])
    assert not result.ok and result.reported_error_code == expected
    assert result.effect_outcome == 'not_started'
    assert result.operation.status == 'failed'
    facts = json.loads(result.output)
    assert facts['http_status'] == 200 and facts['provider_code'] == provider_code


def test_future_cycle_is_independent_without_clearing_prior_unknown(tmp_path, cloud_server):
    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(agent, agent)
    _active(agent)
    assert _call(agent, 'sheet_read', range='sid!A1:B1').ok
    cloud_server.fail_http = True
    first = _execute(agent, 'sheet_append')
    assert not first.ok and first.effect_outcome == 'unknown'
    old_root = agent._current_run_params.task_id
    _active(agent)
    assert agent._current_run_params.task_id != old_root
    cloud_server.fail_http = False
    next_cycle = _execute(agent, 'sheet_append', refresh_attempt=True, operation_id='new-cycle-operation')
    assert next_cycle.ok
    assert len(cloud_server.rows) == 2
    with agent.subagents.runtime_db._runtime_connection() as conn:
        original = conn.execute('SELECT status FROM tool_operations WHERE operation_id=?', ('cloud-original-operation',)).fetchone()
    assert original['status'] == 'UNKNOWN'


def test_readonly_takeover_does_not_reexecute_on_safe_to_retry_result(tmp_path):
    from agent_py_agent.agent.tooling.models import (
        BaseTool,
        ToolHandlerOutcome,
        ToolOperationReconciliation,
    )
    from agent_py_agent.tests._tool_runtime_harness import (
        make_test_model_spec,
        make_test_runtime_policy,
    )

    class SyntheticWrite(BaseTool):
        model_spec = make_test_model_spec('synthetic_unknown_write')
        runtime_policy = make_test_runtime_policy('mutating', idempotency_scope='operation')

        def __init__(self):
            self.calls = 0

        def execute(self, _params):
            self.calls += 1
            return ToolHandlerOutcome('synthetic_unknown_write', False, 'synthetic unknown', effect_outcome='unknown')

        def reconcile_operation(self, _params, _context):
            return ToolOperationReconciliation(outcome='safe_to_retry', source_ref='synthetic://provider-proof')

    agent = _agent(tmp_path, OwnerIdentity.local_main())
    _active(agent)
    tool = SyntheticWrite()
    agent.tools.register(tool)
    current = agent._current_run_params
    for _ in range(2):
        attempt = _authority_attempt_id(agent, current.run_id, task_id=current.task_id)
        result = execute_registry_test_call(agent.tools, 'synthetic_unknown_write', {},
            run_id=current.run_id, operation_id='original-unsafe-write', attempt_id=attempt,
            write_boundary={'task_id':current.task_id}, register_with=agent)
        assert not result.ok and result.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert tool.calls == 1
    assert not result.handler_executed
