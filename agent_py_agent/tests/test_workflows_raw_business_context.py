"""原模型raw run消费宿主冻结DTO，JSON只能声明一致选择，不能签发业务权限。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.tests.test_business_v12_background_scheduling import assigned_task, ok
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native
from agent_py_agent.tests.test_business_v12_workflow_native_run import _flow, _script


@pytest.mark.parametrize('selection', ['omitted', 'scope', 'space'])
def test_raw_model_run_preserves_frozen_context(native, selection):
    _, agent = native
    _script(agent)
    identity, task_id = assigned_task(native, 'raw-workflow')
    task = agent.conversation_store.session_tasks.load(task_id)
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    space = context.space_id
    ok(native, 'workflows.create', _flow('raw-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='raw-flow-create-1')
    tool = agent.tools.tools['workflows']
    params = RunParams(run_id='raw-caller', business_context=context, save=False,
        task_attributes={'conversation_thread_id': identity['agent_id']})
    with host_turn_scope(agent, '', params, reason='raw_run_test', tool_rounds=0):
        selected = {'space_id': space} if selection == 'space' else (
            {'scope': {'scope_mode': 'spaces', 'space_ids': [space]}} if selection == 'scope' else {})
        result = tool.execute({'action': 'run', 'workflow_id': 'raw-flow', **selected})
    assert result.ok, result.output
    record = tool._engine().store.load_run(result.result_envelope['workflow_run_id'])
    assert record.business_context == context.to_dict()
    assert record.scope_snapshot['space_ids'] == [space]
    assert record.steps[0].output_snapshot == {'total': 3}
    original = tool.business_binding_provider
    with host_turn_scope(agent, '', replace(params, run_id='raw-conflicts'), reason='raw_run_test', tool_rounds=0):
        for conflict in ({'space_id': 'other-space'}, {'project_ref': 'other-project'}):
            refused = tool.execute({'action': 'run', 'workflow_id': 'raw-flow', **conflict})
            assert not refused.ok and 'WORKFLOW_CONTEXT_MISMATCH' in refused.output
            assert refused.effect_outcome == 'not_started'
        other = ok(native, 'spaces.create', {'name': 'wrong-scope'}, idempotency_key='wrong-scope-create-1')['space_id']
        ok(native, 'workflows.create', _flow('wrong-scope', scope={'scope_mode': 'spaces', 'space_ids': [other]}),
           idempotency_key='wrong-scope-flow-create-1')
        refused = tool.execute({'action': 'run', 'workflow_id': 'wrong-scope'})
        assert not refused.ok and 'WORKFLOW_SPACE_MISMATCH' in refused.output
        def denied():
            service, actor = original()
            return service, replace(actor, authorize=lambda action, facts: action != 'workflows.run')
        tool.business_binding_provider = denied
        refused = tool.execute({'action': 'run', 'workflow_id': 'raw-flow'})
        assert not refused.ok and 'BUSINESS_WORKFLOW_ACCESS_DENIED' in refused.output
        assert refused.effect_outcome == 'not_started'
    tool.business_binding_provider = original
    assert len(tool._engine().store.list_runs(limit=20)) == 1


def test_raw_frontend_explicit_space_uses_original_factory_and_preserves_pin(native):
    from agent_py_agent.agent.assets.revision_history import latest_version

    _, agent = native
    _script(agent)
    space = ok(native, 'spaces.create', {'name': 'frontend'}, idempotency_key='frontend-space-create-1')['space_id']
    ok(native, 'workflows.create', _flow('frontend-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='frontend-flow-create-1')
    tool = agent.tools.tools['workflows']
    # 未显式选择、无原任务DTO：不可拿当前/唯一空间偷偷补齐。
    refused = tool.execute({'action': 'run', 'workflow_id': 'frontend-flow'})
    assert not refused.ok and 'WORKFLOW_SPACE_MISMATCH' in refused.output
    version = latest_version(tool.owner_home, 'workflow', 'frontend-flow')['version']
    current = tool._store().get_workflow('frontend-flow')
    tool._store().save_workflow(replace(current, steps=(replace(current.steps[0], script_id='missing-current'),)))
    result = tool.execute({'action': 'run', 'workflow_id': 'frontend-flow', 'space_id': space, 'version': version})
    assert result.ok, result.output
    record = tool._engine().store.load_run(result.result_envelope['workflow_run_id'])
    assert record.definition_version == version
    assert record.business_context['space_id'] == space
    assert record.business_context['task_id'] == record.workflow_run_id
    assert record.steps[0].output_snapshot == {'total': 3}
    snapshot = tool._engine().store.load_workflow_snapshot(record.workflow_run_id)
    assert snapshot['_business_context_origin'] == 'workflow_run'
    ok(native, 'workflows.create', _flow('owner-global'), idempotency_key='raw-global-create-1')
    global_result = tool.execute({'action': 'run', 'workflow_id': 'owner-global'})
    assert global_result.ok, global_result.output
    global_record = tool._engine().store.load_run(global_result.result_envelope['workflow_run_id'])
    assert global_record.business_context == {} and global_record.scope_snapshot['scope_mode'] == 'owner_global'


def test_qualified_model_run_inherits_original_task_instead_of_factory_resigning(native):
    _, agent = native
    _script(agent)
    identity, task_id = assigned_task(native, 'qualified-workflow')
    task = agent.conversation_store.session_tasks.load(task_id)
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    ok(native, 'workflows.create', _flow('qualified-flow', scope={'scope_mode': 'spaces',
        'space_ids': [context.space_id]}), idempotency_key='qualified-flow-create-1')
    tool = agent.tools.tools['workflows']
    service, _actor = tool._execution_business_binding()
    calls = []
    def forbidden_reselection(*args, **kwargs):
        calls.append(1)
        raise AssertionError('冻结任务不能调用provider重新签发上下文')
    service.context_provider = forbidden_reselection
    params = RunParams(run_id='qualified-caller', business_context=context, save=False,
        task_attributes={'conversation_thread_id': identity['agent_id']})
    with host_turn_scope(agent, '', params, reason='qualified_run_test', tool_rounds=0):
        result = tool.execute({'action': 'workflows.run', 'workflow_ref': 'qualified-flow',
            'space_id': context.space_id, 'idempotency_key': 'qualified-once-1'})
        assert result.ok, result.output
        refused = tool.execute({'action': 'workflows.run', 'workflow_ref': 'qualified-flow',
            'space_id': 'other-space', 'idempotency_key': 'qualified-conflict-1'})
        assert not refused.ok and 'WORKFLOW_SPACE_MISMATCH' in refused.output
    record = tool._engine().store.load_run(result.result_envelope['workflow_run_id'])
    assert record.business_context == context.to_dict()
    assert record.business_context['task_id'] == task_id != record.workflow_run_id
    assert calls == [] and service.context_provider is forbidden_reselection
    assert '_business_context_origin' not in tool._engine().store.load_workflow_snapshot(record.workflow_run_id)
    second = ok(native, 'tasks.assign', {'agent_id': identity['agent_id'], 'space_id': context.space_id,
        'instruction': 'independent next task'}, idempotency_key='qualified-second-task-1')
    next_context = BusinessExecutionContext.from_dict(
        agent.conversation_store.session_tasks.load(second['task_id']).metadata['business_context'])
    with host_turn_scope(agent, '', replace(params, business_context=next_context), reason='qualified_next', tool_rounds=0):
        replay = tool.execute({'action': 'workflows.run', 'workflow_ref': 'qualified-flow',
            'space_id': context.space_id, 'idempotency_key': 'qualified-once-1'})
        assert not replay.ok and 'WORKFLOW_CONTEXT_MISMATCH' in replay.output
    assert len(tool._engine().store.list_runs(limit=20)) == 1


def test_raw_run_accepts_existing_two_argument_host_context_provider(native):
    _, agent = native
    _script(agent)
    identity, task_id = assigned_task(native, 'injected-provider')
    context = BusinessExecutionContext.from_dict(agent.conversation_store.session_tasks.load(task_id).metadata['business_context'])
    ok(native, 'workflows.create', _flow('injected-flow', scope={'scope_mode': 'spaces',
        'space_ids': [context.space_id]}), idempotency_key='injected-flow-create-1')
    tool = agent.tools.tools['workflows']
    service, _actor = tool._execution_business_binding()
    seen = []
    def provider(actor, request):
        seen.append((actor.owner_id, dict(request)))
        return context
    agent.business_context_provider = provider
    agent.workflow_engine_factory = tool._business_engine
    result = tool.execute({'action': 'run', 'workflow_id': 'injected-flow', 'space_id': context.space_id})
    assert result.ok, result.output
    record = tool._engine().store.load_run(result.result_envelope['workflow_run_id'])
    assert record.business_context == context.to_dict() and seen == [(context.owner_id, {'space_id': context.space_id})]
    assert '_business_context_origin' not in tool._engine().store.load_workflow_snapshot(record.workflow_run_id)


def test_local_business_cache_refreshes_original_registered_workflow_ports(native):
    from agent_py_agent.agent.gateway_parts.business_api import _local_business_services
    from agent_py_agent.agent.workflows.business_scope import WorkflowRunContextFactory

    _, agent = native
    tool = agent.tools.tools.pop('workflows')
    cold = _local_business_services(agent)
    assert cold.workflows.context_provider is None and cold.workflows.engine_factory is None
    agent.tools.register(tool)
    ready = _local_business_services(agent)
    assert ready is not cold
    assert isinstance(ready.workflows.context_provider, WorkflowRunContextFactory)
    assert callable(ready.workflows.engine_factory)
    assert _local_business_services(agent) is ready


def test_explicit_custom_host_missing_context_port_stays_unavailable(native):
    _, agent = native
    _script(agent)
    space = ok(native, 'spaces.create', {'name': 'custom-host'}, idempotency_key='custom-host-space-1')['space_id']
    ok(native, 'workflows.create', _flow('custom-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='custom-host-flow-1')
    tool = agent.tools.tools['workflows']
    agent.workflow_engine_factory = tool._business_engine
    agent.business_context_provider = None
    refused = tool.execute({'action': 'run', 'workflow_id': 'custom-flow', 'space_id': space})
    assert not refused.ok and 'BUSINESS_CONTEXT_UNAVAILABLE' in refused.output
    assert refused.effect_outcome == 'not_started'
    assert tool._engine().store.list_runs(limit=10) == []
