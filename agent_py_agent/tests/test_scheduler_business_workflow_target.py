"""业务API创建的固定版本工作流经原定时工具执行器复用实时权限门。"""
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.assets.revision_history import latest_version
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case


@pytest.mark.parametrize('revoked', [False, True])
def test_api_business_workflow_scheduled_target_keeps_scope_and_current_authority(tmp_path, revoked):
    case = asset_case(tmp_path, 'workflow')
    tool = case.agent.tools.tools['workflows']
    definition = {'descriptor': {'id': 'business_scheduled', 'kind': 'workflow',
        'name': '业务定时工作流', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'one', 'name': '执行', 'mode': 'deterministic_script', 'script_id': 'scheduled_script'}]}
    created = tool.execute({'action': 'workflows.create', 'definition': definition,
        'scope': {'scope_mode': 'owner_global', 'space_ids': []}, 'idempotency_key': 'business-create-one'})
    assert created.ok, created.output
    case.agent._current_run_params.request_id = 'business-schedule-target'
    with patch('agent_py_agent.agent.scheduler.tool.time.time', return_value=900):
        scheduled = ScheduleTool(case.agent).execute({'action': 'create', 'name': '业务固定版本',
            'schedule_kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC',
            'execution_target': {'kind': 'workflow', 'asset_id': 'business_scheduled',
                'input_ref': {'inputs': {'value': 7}}}, 'delivery': {'mode': 'none'}})
    assert scheduled.ok, scheduled.output
    job = next(row for row in case.agent.scheduler_repository.list_jobs()[0]
               if row['execution_target']['asset_id'] == 'business_scheduled')
    pinned_version = latest_version(case.agent.home_paths.owner_home_dir, 'workflow', 'business_scheduled')['version']
    current = case.workflows.get_workflow('business_scheduled')
    case.workflows.save_workflow(replace(current, steps=(replace(current.steps[0], script_id='absent-current'),)))
    if revoked:
        original = tool.business_binding_provider
        def denied():
            service, actor = original()
            return service, replace(actor, authorize=lambda action, facts: action != 'workflows.run')
        tool.business_binding_provider = denied
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    rows, errors = case.agent.scheduler_repository.history()
    row = next(item for item in rows if item['job_id'] == job['job_id'])
    assert not errors
    if revoked:
        assert row['status'] == 'failed'
        assert 'BUSINESS_WORKFLOW_ACCESS_DENIED' in json.dumps(row), (row['error_code'], row.get('error_message'), row.get('structured_result'))
    else:
        assert row['status'] == 'done', row
        workflow_run = row['structured_result']
        assert workflow_run['scope_snapshot']['scope_mode'] == 'owner_global'
        assert workflow_run['definition_version'] == pinned_version
        persisted = WorkflowRunStore(case.agent.home_paths.owner_home_dir).load_run(workflow_run['workflow_run_id'])
        assert persisted.scope_snapshot == workflow_run['scope_snapshot']
        assert persisted.final_output == {'ok': True, 'value': 7}


def test_raw_resume_rechecks_business_permission(tmp_path):
    case = asset_case(tmp_path, 'workflow', script_code='import json\nprint(json.dumps({"_pause_for_resume":True,"value":7}))')
    tool = case.agent.tools.tools['workflows']
    definition = {'descriptor': {'id': 'business_paused', 'kind': 'workflow', 'name': '待恢复业务',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'steps': [{'step_id': 'one', 'name': '执行', 'mode': 'deterministic_script', 'script_id': 'scheduled_script'}]}
    assert tool.execute({'action': 'workflows.create', 'definition': definition,
        'scope': {'scope_mode': 'owner_global', 'space_ids': []}, 'idempotency_key': 'paused-create'}).ok
    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    def call(params):
        with host_turn_scope(case.agent, '', RunParams(run_id='raw-' + params['action'],
                request_id='raw-' + params['action'], save=False), reason='raw_workflow_test', tool_rounds=1) as turn:
            result = tool.execute(params)
            turn.runtime_status = 'ok' if result.ok else 'failed'
            return result
    first = call({'action': 'run', 'workflow_id': 'business_paused'})
    assert first.ok, first.output
    record = json.loads(first.output)
    assert record['status'] == 'paused_for_resume'
    original = tool.business_binding_provider
    def denied():
        service, actor = original()
        return service, replace(actor, authorize=lambda action, facts: action != 'workflows.run')
    tool.business_binding_provider = denied
    result = call({'action': 'resume', 'workflow_run_id': record['workflow_run_id']})
    assert not result.ok and 'BUSINESS_WORKFLOW_ACCESS_DENIED' in result.output
    current = WorkflowRunStore(case.agent.home_paths.owner_home_dir).load_run(record['workflow_run_id'])
    assert current.status == 'paused_for_resume' and current.revision == record['revision']


@pytest.mark.parametrize('channel', ['chat', 'tui'])
def test_failed_target_appends_original_local_chat_with_real_delivery(tmp_path, channel):
    from agent_py_agent.agent.delivery.registry import ChannelAdapterRegistry
    from agent_py_agent.agent.delivery.service import DeliveryService
    case = asset_case(tmp_path, origin_channel=channel, delivery={'mode': 'origin'},
        script_code='raise RuntimeError("local-failure-notice")')
    delivery = DeliveryService(ChannelAdapterRegistry())
    case.agent.delivery_service = delivery
    case.scheduler.runtime.channels = delivery
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = case.agent.scheduler_repository.queued_runs(now=1000)[0]
    case.scheduler.tick(now=1001)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and rows[0]['status'] == 'failed'
    source = case.agent.conversation_store.messages.recent(run['origin_thread_id'], limit=20)
    execution = case.agent.conversation_store.messages.recent(rows[0]['thread_id'], limit=20)
    assert len(source) == 1 and source[0].role == 'assistant', (rows[0], source, execution)
    assert 'local-failure-notice' in source[0].content
    if rows[0]['thread_id'] != run['origin_thread_id']:
        assert not execution


def test_raw_resume_missing_run_keeps_explicit_missing_error(tmp_path):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    result = WorkflowsTool(tmp_path).execute({'action': 'resume', 'workflow_run_id': 'missing-run'})
    assert not result.ok
    assert 'AttributeError' not in result.output
    assert '不存在' in result.output or 'not found' in result.output.lower()
