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
