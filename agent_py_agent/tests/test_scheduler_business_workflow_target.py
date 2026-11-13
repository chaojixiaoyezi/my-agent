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
@pytest.mark.parametrize('execution_mode', ['followup', 'isolated'])
def test_api_business_workflow_scheduled_target_keeps_scope_and_current_authority(tmp_path, revoked, execution_mode):
    case = asset_case(tmp_path, 'workflow', origin_channel='chat')
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
            'execution': {'mode': execution_mode},
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


def test_local_isolated_source_keeps_frozen_binding_without_creating_im_actor(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.execution_threads import EXECUTION_ORIGIN_KEY
    from agent_py_agent.agent.gateway_parts.business_api import _model_business_actor
    from agent_py_agent.agent.scheduler.execution_policy import (
        scheduled_execution_metadata,
        scheduled_run_actor,
    )

    case = asset_case(tmp_path, origin_channel='chat')
    store = case.agent.conversation_store
    origin = store.threads.require(case.job['thread_id'])
    binding = origin.channel_bindings[0]
    run = {'run_id': 'srun-local-origin', 'origin_thread_id': origin.thread_id,
           'creator': {'local': True}, 'owner': case.job['owner'],
           'execution_context': {'origin_thread_id': origin.thread_id, 'origin_bindings': [asdict(binding)]}}
    store.threads.write(replace(origin, channel_bindings=(
        replace(binding, channel_user_id='later-user', channel_conversation_id='later-chat'), binding)))
    assert scheduled_run_actor(case.agent.scheduler_service, run) is None
    metadata = scheduled_execution_metadata(case.agent.scheduler_service, run)
    isolated = replace(origin, thread_id='thread-local-isolated', channel_bindings=(), metadata=metadata)
    store.threads.write(isolated)
    host = SimpleNamespace(conversation_store=store, home_paths=case.agent.home_paths,
        _current_run_params=RunParams(request_id=run['run_id'],
            task_attributes={'conversation_thread_id': isolated.thread_id}))
    actor = _model_business_actor(host)
    assert actor.source_thread_id == origin.thread_id
    assert actor.actor_id == binding.channel_user_id
    assert actor.conversation_id == binding.channel_conversation_id
    assert metadata[EXECUTION_ORIGIN_KEY]['operation_key'] == run['run_id']
    run['execution_context']['origin_bindings'][0]['channel_user_id'] = 'forged'
    from agent_py_agent.agent.conversation.host_actor import HostActorError
    with pytest.raises(HostActorError, match='身份冲突'):
        scheduled_execution_metadata(case.agent.scheduler_service, run)


@pytest.mark.parametrize('outcome', ['not_started', 'unknown', 'committed'])
def test_workflow_host_denial_retains_reason_and_effect(tmp_path, outcome):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    error = HostAPIError('CAPABILITY_DENIED', 403, outcome,
        'BUSINESS_ACTOR_SOURCE_MISSING。执行来源缺失，请从原会话重新发起。')
    with patch('agent_py_agent.agent.tooling.workflows_tool._dispatch', side_effect=error):
        result = WorkflowsTool(tmp_path).execute({'action': 'list'})
    assert not result.ok
    assert result.error_code == 'TOOL_PERMISSION_DENIED'
    assert result.effect_outcome == ('not_started' if outcome == 'not_started' else 'unknown')
    data = json.loads(result.output)
    assert data['error_code'] == 'CAPABILITY_DENIED'
    assert data['error'] == error.message
    assert data['host_error']['outcome'] == outcome
