"""定时复用原宿主业务快照，不从计划正文、当前空间或新版本重选。"""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _business_context_from_wake,
)
from agent_py_agent.agent.scheduler.execution_policy import (
    ScheduledBusinessContextError,
    capture_business_context,
    scheduled_business_context,
)
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.tests.test_business_v12_background_scheduling import assigned_task
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native


@pytest.fixture
def frozen_job(native):
    _, agent = native
    identity, task_id = assigned_task(native, 'schedule-context')
    task = agent.conversation_store.session_tasks.require(task_id)
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    repository = agent.subagents.runtime_db
    proof = repository.record_run_creation(owner_id=agent.home_paths.owner_id,
        run_id='schedule-context-parent', thread_id=task.target_thread_id,
        task_run_metadata={'business_context': context.to_dict(), 'session_task_ref': task_id})
    params = RunParams(run_id='schedule-context-parent', attempt_id=proof['attempt_id'],
        request_id='schedule-context-request', task_id=task_id, business_context=context,
        task_attributes={'conversation_thread_id': task.target_thread_id}, save=False)
    with host_turn_scope(agent, '', params, reason='scheduler_context_test', tool_rounds=1):
        with patch('agent_py_agent.agent.scheduler.tool.time.time', return_value=900):
            result = ScheduleTool(agent).execute({'action': 'create', 'name': '冻结业务来源',
                'prompt': '不从此正文取得空间或版本权限', 'schedule_kind': 'every', 'every_seconds': 600,
                'anchor_at': 1000, 'timezone': 'UTC', 'delivery': {'mode': 'none'},
                'execution': {'mode': 'followup'}})
    assert result.ok, result.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    agent.scheduler_service.monitor_jitter_window_seconds = 0
    agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = agent.scheduler_repository.queued_runs(now=1000)[0]
    return agent, context, params, job, run


def test_original_snapshot_survives_job_run_restart_and_wake(frozen_job):
    agent, context, _, job, run = frozen_job
    assert job['business_context'] == run['business_context'] == context.to_dict()
    owner = agent.scheduler_repository.owner
    restored = SchedulerRepository(agent.scheduler_repository.root,
        owner_provider=owner['provider'], owner_kind=owner['kind'], owner_id=owner['id'],
        quota_enforcer=agent.scheduler_repository.quota_enforcer)
    assert restored.get_job(job['job_id'])['business_context'] == context.to_dict()
    assert scheduled_business_context(agent, run) == context
    request = BackgroundRunRequest(run['thread_id'], task_id=run['run_id'])
    assert _business_context_from_wake(agent, run['thread_id'], request) == context
    projected = deepcopy(run)
    projected['business_context']['asset_version_refs'] = ['new-version']
    with pytest.raises(ScheduledBusinessContextError, match='SOURCE_CONFLICT'):
        scheduled_business_context(agent, projected)


def test_model_attributes_cannot_supply_or_override_context(frozen_job):
    agent, context, params, _, _ = frozen_job
    agent._current_run_params = replace(params, business_context=None,
        task_attributes={'business_context': context.to_dict()})
    assert capture_business_context(agent) is None
    agent._current_run_params = replace(params, business_context=replace(context, space_id='other'))
    with pytest.raises(ScheduledBusinessContextError, match='SOURCE_CONFLICT'):
        capture_business_context(agent)
    agent._current_run_params = replace(params, attempt_id='wrong-attempt')
    with pytest.raises(ScheduledBusinessContextError, match='SOURCE_INVALID'):
        capture_business_context(agent)


def test_space_revocation_blocks_original_snapshot(frozen_job):
    agent, context, _, _, run = frozen_job
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    _, services = local_business_context(agent)
    state = services.spaces._read()
    state['spaces'][context.space_id]['state'] = 'archived'
    with patch.object(services.spaces, '_read', return_value=state):
        with pytest.raises(ValueError, match='SPACE_ARCHIVED'):
            scheduled_business_context(agent, run)


def test_legacy_no_context_job_does_not_inherit_latest(frozen_job):
    agent, _, _, job, run = frozen_job
    legacy = {**run, 'business_context': None}
    original = agent.scheduler_repository.get_job
    agent.scheduler_repository.get_job = lambda *a, **k: {**job, 'business_context': None}
    try:
        assert scheduled_business_context(agent, legacy) is None
    finally:
        agent.scheduler_repository.get_job = original
    with pytest.raises(ScheduledBusinessContextError, match='OWNER_MISMATCH'):
        scheduled_business_context(agent, {**run, 'owner': {'id': 'foreign'}})


def test_runtime_strong_gate_requires_exact_claimed_run(frozen_job):
    from agent_py_agent.agent.agent_core.runtime_mixin import _verified_business_task_metadata

    agent, context, _, _, run = frozen_job
    params = RunParams(task_id=run['run_id'], source='scheduler_tool', business_context=context,
        task_attributes={'conversation_thread_id': run['thread_id']})
    with pytest.raises(ScheduledBusinessContextError, match='RUN_UNCLAIMED'):
        _verified_business_task_metadata(agent, params, agent.home_paths.owner_id)
    claimed = agent.scheduler_repository.claim_run(run['run_id'], lease_seconds=300, now=1001)
    assert claimed is not None
    assert _verified_business_task_metadata(agent, params, agent.home_paths.owner_id) == {
        'business_context': context.to_dict(), 'session_task_ref': context.task_id}
    original_task = agent.conversation_store.session_tasks.require(context.task_id)
    with host_turn_scope(agent, '', replace(params, request_id=run['run_id'], run_id=run['run_id']),
            reason='scheduler_bound_identity_test', tool_rounds=1) as turn:
        assert turn.params.business_context == context
        assert agent.subagents.runtime_db.business_context_for_execution(
            owner_id=agent.home_paths.owner_id, run_id=turn.params.run_id,
            attempt_id=turn.params.attempt_id) == context
    assert agent.conversation_store.session_tasks.require(context.task_id) == original_task
    with pytest.raises(ScheduledBusinessContextError, match='RUN_UNCLAIMED'):
        _verified_business_task_metadata(agent, replace(params,
            task_attributes={'conversation_thread_id': 'other-thread'}), agent.home_paths.owner_id)


def test_nonempty_version_transport_is_immutable(frozen_job):
    from agent_py_agent.agent.conversation.business_contracts import BusinessPackageVersion
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        _new_job_payload,
        _reserved_run,
    )

    agent, context, _, job, _ = frozen_job
    # 只验完整DTO运输，不把合成ref冒称实际采纳或包执行。
    context = replace(context, asset_version_refs=('synthetic-v1',),
        selected_method_refs=('capability:synthetic-pack',),
        package_versions=(BusinessPackageVersion('capability:synthetic-pack', 'digest-v1', 'activation-v1'),),
        package_versions_state='frozen')
    created = _new_job_payload(owner=agent.scheduler_repository.owner, now=900,
        request=SchedulerJobCreateRequest(name='版本运输', prompt='读取原版本', thread_id=job['thread_id'],
            schedule=job['schedule'], business_context=context.to_dict()))
    run = _reserved_run(created, trigger='schedule', scheduled_for=1000, dispatch_after=1000, now=1000)
    assert run['business_context'] == context.to_dict()
    created['business_context']['asset_version_refs'][:] = ['v2']
    created['business_context']['package_versions'][0]['version_ref'] = 'digest-v2'
    assert run['business_context'] == context.to_dict()
