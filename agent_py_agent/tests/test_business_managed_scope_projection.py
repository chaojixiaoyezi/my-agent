"""受管业务执行的逐行及直接访问复用同一冻结范围；source标签不授权。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native
from agent_py_agent.tests.test_scheduler_business_context_freeze import frozen_job as frozen_job


@pytest.mark.parametrize('source', ['background_main_agent', 'gateway'])
def test_managed_scope_filters_before_limit_and_rejects_other_space_read_write(frozen_job, source):
    agent, frozen, params, _, _ = frozen_job
    # 另一行是同Owner可管理的空间，不把拥有者本来权限错误地当成任务跨空间委托。
    from agent_py_agent.agent.gateway_parts.business_api import (
        local_business_context as local_context,
    )
    context, services = local_context(agent)
    actor = services.actor(context)
    other = services.spaces.create({'name': 'another-private-scope', 'purpose': 'synthetic other project body'},
        actor=actor, idempotency_key='managed-projection-other-space')
    params = replace(params, source=source)
    with host_turn_scope(agent, '', params, reason='managed_scope_test', tool_rounds=1):
        context, services = local_business_context(agent, model=True)
        actor = services.actor(context)
        listed = services.spaces.list({'limit': 1}, actor=actor)
        assert [row['space_id'] for row in listed['items']] == [frozen.space_id]
        assert listed['next_cursor'] is None
        for action, values in [('spaces.get', {'space_id': other['space_id']}),
                               ('spaces.update', {'space_id': other['space_id'], 'purpose': 'must not write'})]:
            with pytest.raises(BusinessError) as rejected:
                services.spaces.call(action, values, actor=actor, expected_revision=other['revision'],
                    idempotency_key='managed-projection-denied-' + action)
            assert rejected.value.code == 'BUSINESS_ACCESS_DENIED'
            assert rejected.value.effect_outcome == 'not_started'
            assert rejected.value.details['reason'] == 'frozen_task_business_scope_mismatch'
    # 退出受管执行后，原正常管理入口仍能看两个空间，且拒绝的更新没改内容。
    context, services = local_context(agent)
    actor = services.actor(context)
    assert services.spaces.get(other['space_id'], actor=actor)['space']['purpose'] == 'synthetic other project body'
    assert len(services.spaces.list({}, actor=actor)['items']) == 2


@pytest.mark.parametrize('fault', ['different_space', 'stale_attempt', 'missing_context', 'cross_owner', 'untyped_context'])
def test_model_cannot_replace_canonical_managed_scope_or_claim_gateway(frozen_job, fault):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    agent, frozen, params, _, _ = frozen_job
    forged = replace(params, source='gateway')
    if fault == 'different_space':
        forged = replace(forged, business_context=replace(frozen, space_id='space-not-the-original'))
    elif fault == 'stale_attempt':
        forged = replace(forged, attempt_id='stale-attempt')
    elif fault == 'cross_owner':
        forged = replace(forged, business_context=replace(frozen, owner_id='local/foreign'))
    elif fault == 'untyped_context':
        with pytest.raises(TypeError, match='BusinessExecutionContext'):
            replace(forged, business_context=frozen.to_dict())
        return
    else:
        forged = replace(forged, business_context=None)
    previous = getattr(agent, '_current_run_params', None)
    agent._current_run_params = forged
    try:
        with pytest.raises(HostAPIError) as denied:
            local_business_context(agent, model=True)
        assert denied.value.code == 'CAPABILITY_DENIED'
    finally:
        agent._current_run_params = previous


def test_original_foreground_management_keeps_two_space_discovery_and_dispatch(frozen_job):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams

    agent, frozen, _, _, _ = frozen_job
    context, services = local_business_context(agent)
    actor = services.actor(context)
    other = services.spaces.create({'name': 'foreground-second-scope', 'purpose': 'other owner-authorized purpose'},
        actor=actor, idempotency_key='foreground-scope-other')
    original = agent.conversation_store.session_tasks.require(frozen.task_id)
    run_id = 'foreground-native-management'
    proof = agent.subagents.runtime_db.record_run_creation(owner_id=agent.home_paths.owner_id,
        run_id=run_id, thread_id=original.sender_thread_id, task_run_metadata={})
    params = RunParams(run_id=run_id, task_id=run_id, request_id=run_id, attempt_id=proof['attempt_id'],
        source='gateway', task_attributes={'conversation_thread_id': original.sender_thread_id})
    with host_turn_scope(agent, '', params, reason='foreground_scope_test', tool_rounds=1):
        context, services = local_business_context(agent, model=True)
        assert context.business_context is None
        assert len(services.spaces.list({}, actor=services.actor(context))['items']) == 2
        receipts = [services.invoke('tasks.submit', {'space_id': sid, 'instruction': 'one isolated work item'}, context,
            idempotency_key='foreground-dispatch-' + sid) for sid in (frozen.space_id, other['space_id'])]
    tasks = [agent.conversation_store.session_tasks.require(row['task_id']) for row in receipts]
    assert tasks[0].target_thread_id == tasks[1].target_thread_id
    assert [task.metadata['business_context']['space_id'] for task in tasks] == [frozen.space_id, other['space_id']]


def test_missing_both_runtime_scope_copies_cannot_erase_original_session_task_scope(frozen_job):
    import json

    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    agent, frozen, params, _, _ = frozen_job
    repository = agent.subagents.runtime_db
    with repository.transaction() as connection:
        row = connection.execute('SELECT tr.task_run_id,tr.metadata_json FROM agent_runs ar '
            'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id WHERE ar.run_id=?', (params.run_id,)).fetchone()
        metadata = json.loads(row['metadata_json'])
        assert metadata['session_task_ref'] == frozen.task_id
        metadata.pop('business_context')
        connection.execute('UPDATE task_runs SET metadata_json=? WHERE task_run_id=?',
            (json.dumps(metadata), row['task_run_id']))
    previous = getattr(agent, '_current_run_params', None)
    agent._current_run_params = replace(params, business_context=None, source='gateway')
    try:
        with pytest.raises(HostAPIError, match='BUSINESS_CONTEXT_EXECUTION_MISMATCH'):
            local_business_context(agent, model=True)
    finally:
        agent._current_run_params = previous
    assert agent.conversation_store.session_tasks.require(frozen.task_id).metadata['business_context'] == frozen.to_dict()
