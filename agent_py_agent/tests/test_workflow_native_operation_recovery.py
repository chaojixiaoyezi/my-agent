"""原生操作后到终态只读恢复；普通请求不能制造核验事实。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.local_storage import ToolOperationCompletionRequest
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (  # noqa: F401
    CounterTool,
    flow,
    host,
    wire,
)


def unknown_write(wire, tmp_path):  # noqa: F811
    agent, client = wire
    target = tmp_path / 'effect.txt'
    tool = CounterTool()
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy(mode='never'),
        effect_resolver=EffectResolverPolicy(default_effect='mutating'),
        idempotency_policy=IdempotencyPolicy(scope='operation'))
    def execute(params):
        tool.calls.append(params)
        target.write_text('once')
        raise TimeoutError('target committed before response')
    tool.execute = execute
    agent.tools.register(tool)
    definition = flow('native-recovery', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    assert client.call('workflows.create', {'definition': definition})['ok']
    first = client.call('workflows.run', {'workflow_ref': 'native-recovery'})
    assert first['ok'], first
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run = store.load_run(first['data']['workflow_run_id'])
    assert run.steps[0].error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN', run.to_dict()
    binding = run.steps[0].escalation_result['tool_operation_binding']
    return agent, client, store, run, binding, tool, target


def test_late_native_operation_completion_recovers_without_second_write(wire, tmp_path, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.workflows import governance
    agent, client = wire
    original_bind = governance.bind_workflow_tool_operation

    def finish_after_observed_timeout(ops, run, step, call, operation, **kwargs):
        original_bind(ops, run, step, call, operation, **kwargs)
        binding = step.escalation_result['tool_operation_binding']
        original = ops.get_tool_operation(owner_id=run.owner_id, run_id=binding['run_id'], operation_id=binding['operation_id'])
        # 原执行轮仍活跃时，模拟其超时后迟到的真实完成。仅经原holder/generation完成API落账。
        result = {**original.result, 'ok': True, 'output': json.dumps({'value': 7}),
            'handler_executed': True, 'effect_outcome': 'committed', 'error_code': '', 'failure_stage': ''}
        ops.finish_tool_operation(ToolOperationCompletionRequest(owner_id=original.owner_id,
            run_id=original.run_id, operation_id=original.operation_id, holder_id=original.holder_id,
            generation=original.generation, status='succeeded', result=result))

    monkeypatch.setattr(governance, 'bind_workflow_tool_operation', finish_after_observed_timeout)
    agent, client, store, run, binding, tool, target = unknown_write(wire, tmp_path)
    response = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id},
        expected_revision=run.revision))
    assert response['ok'] and response['data']['run']['status'] == 'succeeded', response
    assert len(tool.calls) == 1 and target.read_text() == 'once'
    assert store.load_run(run.workflow_run_id).steps[0].output_snapshot['effect_outcome'] == 'committed'


@pytest.mark.parametrize('changed', ['unknown', 'owner_id', 'run_id', 'task_id', 'attempt_id', 'args_hash', 'missing'])
def test_unknown_or_mismatched_binding_cannot_confirm(wire, tmp_path, changed):  # noqa: F811
    agent, client, store, run, binding, tool, target = unknown_write(wire, tmp_path)
    if changed == 'missing':
        run.steps[0].escalation_result.pop('tool_operation_binding')
    elif changed != 'unknown':
        binding[changed] = 'forged'
    if changed != 'unknown':
        store.save_run(run, resuming=True)
        run = store.load_run(run.workflow_run_id)
    response = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id},
        expected_revision=run.revision))
    assert not response['ok'] and response['error']['code'] == 'WORKFLOW_RECONCILIATION_REQUIRED', response
    assert response['effect_outcome'] == 'not_started'
    assert len(tool.calls) == 1 and target.read_text() == 'once'
    assert store.load_run(run.workflow_run_id).status == 'failed'


def test_plain_native_run_persists_host_owner_and_exact_write_binding(host):  # noqa: F811
    """普通入口没有业务DTO也必须沿真实宿主身份绑定原写操作。"""
    host._current_run_params.allowed_tools.append('write_file')
    tool = host.tools.tools['workflows']
    target = host.effective_workspace_root / 'native-owned.txt'
    definition = flow('plain-owned', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'tool_spec': {'tool_name': 'write_file', 'arguments': {'path': str(target), 'content': 'once'}}}])
    assert tool.execute({'action': 'save', 'workflow_id': 'plain-owned', 'definition': definition}).ok
    outcome = tool.execute({'action': 'run', 'workflow_id': 'plain-owned'})
    assert outcome.ok, outcome.output
    run = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(outcome.result_envelope['workflow_run_id'])
    assert run.owner_id == host.home_paths.owner_id == 'local/main'
    assert run.business_context == {} and target.read_text() == 'once'
    step = run.steps[0]
    binding = step.escalation_result['tool_operation_binding']
    assert binding['owner_id'] == run.owner_id
    assert binding['tool_name'] == 'write_file' and binding['args_hash']
    assert binding['effect_attempt_id'] == step.escalation_result['attempt_id']
    assert 'tool_operation_binding_error' not in step.escalation_result


@pytest.mark.parametrize('conflict', ['argument', 'descriptor', 'business_context'])
def test_bound_engine_rejects_owner_conflicts_before_run_creation(tmp_path, conflict):
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    definition = flow('conflicting', [])
    options = {}
    if conflict == 'argument':
        options['owner_id'] = 'local/other'
    elif conflict == 'descriptor':
        definition['descriptor']['owner_id'] = 'local/other'
    else:
        options['business_context'] = BusinessExecutionContext('local/other', None, None,
            'task', None, None, (), (), 'business_execution_context.v1')
    engine = WorkflowEngine(tmp_path, owner_id='local/main')
    with pytest.raises(PermissionError, match='WORKFLOW_OWNER_MISMATCH'):
        engine.run_workflow(definition, **options)
    assert engine.store.list_runs(limit=10) == []


@pytest.mark.parametrize('original_owner', ['', 'local/other'])
def test_bound_host_does_not_resign_legacy_or_foreign_run(tmp_path, original_owner):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    definition = flow('legacy-owner', [])
    record = WorkflowEngine(tmp_path).run_workflow(definition, owner_id=original_owner)
    engine = WorkflowEngine(tmp_path, owner_id='local/main')
    with pytest.raises(PermissionError, match='WORKFLOW_OWNER_MISMATCH'):
        engine.resume_workflow_run(record.workflow_run_id)
    persisted = engine.store.load_run(record.workflow_run_id)
    assert persisted.owner_id == original_owner and persisted.revision == record.revision


def test_bound_native_parent_and_child_share_persisted_host_owner(tmp_path):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    store = WorkflowStore(tmp_path)
    store.save_workflow(flow('owned-child', []))
    store.save_workflow(flow('owned-parent', [{'step_id': 'child', 'name': 'child',
        'mode': 'subworkflow', 'subworkflow_ref': 'workflow:owned-child'}]))
    engine = WorkflowEngine(tmp_path, owner_id='local/main')
    parent = engine.run_workflow('owned-parent')
    child = engine.store.load_run(parent.steps[0].subworkflow_run_id)
    assert parent.owner_id == child.owner_id == 'local/main'
    assert child.parent_workflow_run_id == parent.workflow_run_id


def test_permission_refusal_does_not_leave_claim_but_live_callback_still_excludes_peer(host, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.agent_core import self_map_bootstrap
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        run_with_parallel_thread_context,
    )

    tool = host.tools.tools['workflows']
    target = host.effective_workspace_root / 'claim-read.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('once-readable')
    definition = flow('claim-permission', [{'step_id': 'read', 'name': 'read', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': str(target)}}}])
    assert tool.execute({'action': 'save', 'workflow_id': 'claim-permission', 'definition': definition}).ok
    host._current_run_params.allowed_tools.remove('read_file')
    entered, release = Event(), Event()
    original = self_map_bootstrap._WorkflowToolDispatcher.execute_scoped
    def blocked(self, tool_name, arguments, **kwargs):
        value = original(self, tool_name, arguments, **kwargs)
        if value.get('error_code') == 'TOOL_NOT_ALLOWED':
            entered.set()
            assert release.wait(5)
        return value
    monkeypatch.setattr(self_map_bootstrap._WorkflowToolDispatcher, 'execute_scoped', blocked)
    captured = _capture_parallel_thread_context(host)
    def run():
        return tool.execute({'action': 'run', 'workflow_id': 'claim-permission',
            'concurrency_policy': 'skip_if_running'})
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_with_parallel_thread_context, host, captured, run)
        try:
            assert entered.wait(5)
            peer = run()
            assert peer.result_envelope['status'] == 'skipped'
        finally:
            release.set()
        first = future.result(10)
    assert not first.ok
    first_run = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(first.result_envelope['workflow_run_id'])
    assert first_run.steps[0].escalation_result['effect_started'] is False
    assert first_run.steps[0].escalation_result['effect_outcome'] == 'not_started'
    host._current_run_params.allowed_tools.append('read_file')
    following = run()
    assert following.ok and following.result_envelope['status'] == 'succeeded', following.output
    assert first_run.status == 'failed'


def test_unknown_native_write_still_holds_claim_when_later_tool_is_forbidden(host):  # noqa: F811
    host._current_run_params.allowed_tools.append('native_counter')
    target = host.effective_workspace_root / 'unknown-claim.txt'
    calls = []
    counter = CounterTool()
    counter.runtime_policy = replace(counter.runtime_policy,
        approval_policy=ApprovalPolicy(mode='never'),
        effect_resolver=EffectResolverPolicy(default_effect='mutating'),
        idempotency_policy=IdempotencyPolicy(scope='operation'))
    def execute(params):
        calls.append(params)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('once')
        raise TimeoutError('actual write completed, response unavailable')
    counter.execute = execute
    host.tools.register(counter)
    tool = host.tools.tools['workflows']
    definition = flow('unknown-claim', [{'step_id': 'write', 'name': 'unknown write', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    assert tool.execute({'action': 'save', 'workflow_id': 'unknown-claim', 'definition': definition}).ok
    first = tool.execute({'action': 'run', 'workflow_id': 'unknown-claim', 'concurrency_policy': 'skip_if_running'})
    assert not first.ok
    stored = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(first.result_envelope['workflow_run_id'])
    assert stored.steps[0].escalation_result['tool_receipt']['effect_outcome'] == 'unknown'
    assert stored.steps[0].escalation_result['effect_started'] is True
    host._current_run_params.allowed_tools.remove('native_counter')
    following = tool.execute({'action': 'run', 'workflow_id': 'unknown-claim', 'concurrency_policy': 'skip_if_running'})
    assert following.result_envelope['status'] == 'skipped'
    assert len(calls) == 1 and target.read_text() == 'once'


def test_business_item_two_workflow_runs_replay_original_operation_and_cursor(host):  # noqa: F811
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.step_watermarks import load_workflow_step_watermark

    params = host._current_run_params
    params.allowed_tools.append('write_file')
    with host.subagents.runtime_db._runtime_connection() as connection:
        task = connection.execute('SELECT tr.task_id FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id WHERE ar.run_id=?', (params.run_id,)).fetchone()
    params.task_id = task['task_id']
    engine = WorkflowEngine(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id,
        workspace_root=host.effective_workspace_root, run_authorizer=lambda *_: None)
    engine.tool_dispatcher = _WorkflowToolDispatcher(host, params, lambda callback, **_: callback())
    target = host.effective_workspace_root / 'business-once.txt'
    definition = flow('business-once', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'on_error': 'fail', 'config': {'business_effect': {'item_key': {'step_id': '$inputs', 'key': 'item'},
            'configuration_key': 'sink-v1'}, 'watermark_key': 'status'},
        'tool_spec': {'tool_name': 'write_file', 'arguments': {'path': str(target)},
            'arguments_map': {'content': {'step_id': '$inputs', 'key': 'content'}}}}])
    context = BusinessExecutionContext(host.home_paths.owner_id, 'space-a', None,
        'business-task-a', 1, None, (), (), 'business_execution_context.v1')
    first = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'once'}, business_context=context)
    assert first.status == 'succeeded', first.steps[0].error_message
    before = target.stat().st_mtime_ns
    second = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'once'}, business_context=context)
    assert second.status == 'succeeded', second.to_dict()
    assert first.workflow_run_id != second.workflow_run_id
    original = first.steps[0].escalation_result['tool_operation_binding']
    replay = second.steps[0].escalation_result['tool_operation_binding']
    assert replay['operation_id'] == original['operation_id']
    assert replay['run_id'] == original['run_id'] and replay['attempt_id'] == original['attempt_id']
    assert second.steps[0].output_snapshot['operation']['replayed'] is True
    assert target.read_text() == 'once' and target.stat().st_mtime_ns == before
    scope = first.steps[0].escalation_result['business_effect']['scope_key']
    checkpoint = load_workflow_step_watermark(host.home_paths.owner_home_dir, 'business-once', 'write', scope_key=scope)
    assert checkpoint['revision'] == 1 and checkpoint['watermark']['cursor'] == 'succeeded'
    assert second.steps[0].escalation_result['prior_watermark'] == checkpoint

    changed = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'different'}, business_context=context)
    assert changed.status == 'failed' and changed.steps[0].error_code == 'TOOL_OPERATION_IDENTITY_CONFLICT'
    assert target.read_text() == 'once' and target.stat().st_mtime_ns == before
    other_space = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'space-b'},
        business_context=replace(context, space_id='space-b'))
    assert other_space.status == 'succeeded', other_space.to_dict()
    assert other_space.steps[0].escalation_result['tool_operation_binding']['operation_id'] != original['operation_id']
    assert other_space.steps[0].escalation_result['prior_watermark'] is None
    assert target.read_text() == 'space-b'
    definition['steps'][0]['config']['business_effect']['configuration_key'] = 'sink-v2'
    next_config = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'config-v2'}, business_context=context)
    assert next_config.status == 'succeeded', next_config.to_dict()
    assert next_config.steps[0].escalation_result['tool_operation_binding']['operation_id'] != original['operation_id']
    assert next_config.steps[0].escalation_result['prior_watermark'] is None
    params.allowed_tools.remove('write_file')
    engine.tool_dispatcher = _WorkflowToolDispatcher(host, params, lambda callback, **_: callback())
    denied = engine.run_workflow(definition, inputs={'item': 'item-7', 'content': 'config-v2'}, business_context=context)
    assert denied.steps[0].error_code == 'TOOL_NOT_ALLOWED'
    assert 'prior_watermark' not in denied.steps[0].escalation_result
    assert target.read_text() == 'config-v2'


def test_business_item_postwrite_unknown_never_reexecutes_or_advances_cursor(host):  # noqa: F811
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.step_watermarks import load_workflow_step_watermark

    params = host._current_run_params
    with host.subagents.runtime_db._runtime_connection() as connection:
        params.task_id = connection.execute('SELECT tr.task_id FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id WHERE ar.run_id=?', (params.run_id,)).fetchone()['task_id']
    params.allowed_tools.append('native_counter')
    tool = CounterTool()
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy(mode='never'),
        effect_resolver=EffectResolverPolicy(default_effect='mutating'), idempotency_policy=IdempotencyPolicy(scope='operation'))
    marker = host.effective_workspace_root / 'unknown-business-item.txt'
    def execute(arguments):
        tool.calls.append(arguments)
        marker.write_text('real-before-timeout')
        raise TimeoutError('response lost after physical write')
    tool.execute = execute
    host.tools.register(tool)
    engine = WorkflowEngine(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id,
        workspace_root=host.effective_workspace_root, run_authorizer=lambda *_: None)
    engine.tool_dispatcher = _WorkflowToolDispatcher(host, params, lambda callback, **_: callback())
    definition = flow('business-unknown', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'on_error': 'fail', 'config': {'business_effect': {'item_key': 'item-7', 'configuration_key': 'v1'},
            'watermark_key': 'status'}, 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    context = BusinessExecutionContext(host.home_paths.owner_id, 'space-a', None, 'business-task', 1, None, (), (), 'business_execution_context.v1')
    first = engine.run_workflow(definition, business_context=context)
    assert first.steps[0].error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN', first.to_dict()
    before = marker.stat().st_mtime_ns
    second = engine.run_workflow(definition, business_context=context)
    assert second.steps[0].error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN', second.to_dict()
    assert len(tool.calls) == 1 and marker.stat().st_mtime_ns == before
    left = first.steps[0].escalation_result['tool_operation_binding']
    right = second.steps[0].escalation_result['tool_operation_binding']
    assert left['operation_id'] == right['operation_id']
    assert first.steps[0].watermark is None and second.steps[0].watermark is None
    scope = first.steps[0].escalation_result['business_effect']['scope_key']
    assert load_workflow_step_watermark(host.home_paths.owner_home_dir, 'business-unknown', 'write', scope_key=scope) is None


def test_business_item_requires_host_business_scope_before_handler(host):  # noqa: F811
    params = host._current_run_params
    params.allowed_tools.append('write_file')
    target = host.effective_workspace_root / 'must-not-write.txt'
    definition = flow('scope-required-item', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'on_error': 'fail', 'config': {'business_effect': {'item_key': 'item-7', 'configuration_key': 'v1'}},
        'tool_spec': {'tool_name': 'write_file', 'arguments': {'path': str(target), 'content': 'forbidden'}}}])
    tool = host.tools.tools['workflows']
    assert tool.execute({'action': 'save', 'workflow_id': 'scope-required-item', 'definition': definition}).ok
    tool.execute({'action': 'run', 'workflow_id': 'scope-required-item'})
    [run] = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
    step = run.steps[0]
    assert step.error_code == 'WORKFLOW_BUSINESS_EFFECT_SCOPE_REQUIRED'
    assert step.escalation_result['tool_receipt']['effect_outcome'] == 'not_started'
    assert step.escalation_result['effect_started'] is False and not target.exists()
    assert 'prior_watermark' not in step.escalation_result


@pytest.mark.parametrize('field', ['owner_id', 'task_id', 'scope_key', 'item_key'])
def test_business_item_identity_different_authoritative_partition_is_independent(field):
    from agent_py_agent.agent.contracts.idempotency import BusinessEffectIdentity
    identity = BusinessEffectIdentity('owner-a', 'task-a', 'space-config-version-a', 'item-a')
    assert replace(identity, **{field: 'different'}).operation_id != identity.operation_id
