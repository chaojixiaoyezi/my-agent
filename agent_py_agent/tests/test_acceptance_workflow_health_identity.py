"""WF-11 身份切片：纯值合同、真实引擎/注册表，以及默认宿主工具链。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.query_health import (
    build_workflow_health_view,
    summarize_run_step_health,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import CounterTool, flow, local_run
from agent_py_agent.tests.test_acceptance_workflow_native_ports import host as host


def make_run(step):
    return WorkflowRunRecord(workflow_run_id='identity-unit-run', workflow_id='identity-unit',
        owner_id='unit-owner', trigger_kind='manual', status='succeeded', started_at=0.0,
        ended_at=0.0, duration_ms=0.0, steps=(step,))


@pytest.mark.parametrize('mode,effect,escalation,child,expected', [
    ('llm_skill_analysis', 'effect-only', {}, '', ''),
    ('subworkflow', 'child-run', {'subworkflow_receipt': {'child_run_id': 'child-run'}}, 'child-run', ''),
    ('tool_call', 'effect', {'tool_receipt': {'operation_id': 'canonical-operation'}}, '', 'canonical-operation'),
    ('tool_call', 'same-text', {'tool_receipt': {'operation_id': 'same-text'}}, '', 'same-text'),
    # 未证来源的顶层operation可能是非工具领域操作，不能自动当作tool-operation。
    ('subworkflow', 'child-run', {'operation_id': 'child-run'}, 'child-run', ''),
])
def test_health_reference_sources(mode, effect, escalation, child, expected):
    step = StepExecutionRecord(step_id='step', name='step', mode=mode, status='succeeded',
        started_at=0.0, ended_at=0.0, duration_ms=0.0, llm_involved=False, effect_id=effect,
        escalation_result=escalation, subworkflow_run_id=child)
    [summary] = summarize_run_step_health(make_run(step))['steps']
    assert summary['effect_id'] == effect
    assert summary['operation_id'] == expected
    assert summary['subworkflow_run_id'] == child
    assert summary['duration_ms'] == 0.0


class ReceiptTool(BaseTool):
    """仅替换叶端业务体；注册表、引擎、原存储及策略不替换。"""

    model_spec = ToolModelSpec('identity_receipt', '离线回执协议探针',
        {'type': 'object', 'properties': {}, 'additionalProperties': False})
    runtime_policy = ToolRuntimePolicy(EffectResolverPolicy(default_effect='read_only'))

    def __init__(self, protocol, supplied):
        self.protocol, self.supplied = protocol, supplied
        self.calls = 0

    def execute_scoped(self, params, **kwargs):
        return self.execute(params)

    def execute(self, params):
        self.calls += 1
        if self.protocol == 'sequence':
            return {'ok': self.calls != 2, 'value': self.calls, 'error_code': 'TOOL_ERROR' if self.calls == 2 else ''}
        if self.protocol == 'exception':
            raise ConnectionError('offline identity failure')
        if self.protocol == 'scalar':
            return 'offline scalar result'
        ok = not self.protocol.endswith('failure')
        if self.protocol.startswith('typed'):
            outcome = ToolHandlerOutcome('identity_receipt', ok, json.dumps({'value': 7}),
                operation_id=self.supplied, error_code='' if ok else 'TOOL_ERROR')
            # 测试扩展形状：人为附加可序列化result，不代表标准裸Outcome已适配。
            outcome.result = {'value': 7}
            return outcome
        result = {'ok': ok, 'value': 7}
        if self.supplied:
            result['operation_id'] = self.supplied
        if not ok:
            result['error_code'] = 'TOOL_ERROR'
        return result


@pytest.mark.parametrize('protocol', ['typed-success', 'typed-failure', 'dict-success', 'dict-failure', 'scalar', 'exception'])
@pytest.mark.parametrize('supplied', ['', 'receipt-operation'])
def test_real_engine_registry_receipt_identity(tmp_path, protocol, supplied):
    """真实Registry工具对象端口(C分支)，不是默认_WorkflowToolDispatcher端口。"""
    registry = ToolRegistry(ToolRegistryParams(workspace_root=tmp_path, max_chars=1000,
        max_entries=10, max_matches=10, web_max_chars=1000, http_timeout=1,
        catalog_limit=10, retrieval_limit=1, vector_search_enabled=False))
    tool = ReceiptTool(protocol, supplied)
    registry.register(tool)
    engine = WorkflowEngine(tmp_path)
    engine.tool_registry = registry
    definition = WorkflowDefinition(AssetDescriptor(id='identity-flow', kind='workflow',
        name='身份合同', owner_id='unit-owner'), (WorkflowStepSpec(step_id='call', name='call',
        mode='tool_call', on_error='fail', tool_spec={'tool_name': 'identity_receipt'}),))
    WorkflowStore(tmp_path).save_workflow(definition)
    try:
        run = engine.run_workflow(definition)
        step = run.steps[0]
        failed = protocol.endswith('failure') or protocol == 'exception'
        assert run.status == ('failed' if failed else 'succeeded'), run.to_dict()
        assert step.status == run.status
        assert tool.calls == 1
        receipt = step.escalation_result['tool_receipt']
        assert receipt['status'] == step.status
        assert receipt['effect_id'] == step.effect_id
        assert step.effect_id.startswith('workflow:' + run.workflow_run_id + ':call:')
        expected = supplied if protocol not in ('scalar', 'exception') else ''
        assert receipt['operation_id'] == expected
        saved = WorkflowRunStore(tmp_path).load_run(run.workflow_run_id)
        assert saved.steps[0].escalation_result['tool_receipt'] == receipt
        assert summarize_run_step_health(saved)['steps'][0]['operation_id'] == expected
        view = build_workflow_health_view(engine.store, workflow_id='identity-flow', limit=10)
        assert view['canonical_references']['operation_ids'] == ([expected] if expected else [])
        assert view['canonical_references']['effect_ids'] == [step.effect_id]
    finally:
        registry.close_mcp_clients()


@pytest.fixture
def executor_results(host, monkeypatch):
    """只观察原Registry调用返回值；不替换Executor、Store或签发操作身份。"""
    observed = []
    original = host.tools.execute_tool

    def observe(call, **kwargs):
        execution = original(call, **kwargs)
        observed.append((call, execution.result))
        return execution

    monkeypatch.setattr(host.tools, 'execute_tool', observe)
    return observed


def test_default_read_without_operation_survives_health_and_replay(host, executor_results):
    path = host.home_paths.owner_home_dir / 'identity-input.txt'
    path.write_text('identity native input', encoding='utf-8')
    definition = flow('identity-native', [{'step_id': 'read', 'name': 'read', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': str(path)}}}])
    result, run = local_run(host, definition, inputs={})
    assert result.ok and run.status == 'succeeded', run.to_dict()
    [(call, original)] = executor_results
    assert original.ok and original.handler_executed
    assert original.operation is None
    assert call.operation_id == f'workflow-step:{run.workflow_run_id}:read'
    receipt = run.steps[0].escalation_result['tool_receipt']
    assert run.steps[0].output_snapshot['operation'] is None
    assert 'identity native input' in json.dumps(run.steps[0].output_snapshot)
    assert receipt['operation_id'] == ''
    assert receipt['effect_outcome'] == original.effect_outcome
    assert run.steps[0].effect_id
    assert summarize_run_step_health(run)['steps'][0]['operation_id'] == ''
    store = WorkflowRunStore(host.home_paths.owner_home_dir)
    assert build_workflow_health_view(store, workflow_id='identity-native', limit=10)[
        'canonical_references']['operation_ids'] == []
    again = host.tools.tools['workflows'].execute({'action': 'workflows.run',
        'workflow_ref': 'identity-native', 'idempotency_key': 'native-run-0000001', 'inputs': {}})
    assert again.ok
    saved = store.load_run(run.workflow_run_id)
    assert saved.revision == run.revision
    assert saved.steps[0].escalation_result['tool_receipt'] == receipt
    assert saved.steps[0].output_snapshot == run.steps[0].output_snapshot
    assert len(executor_results) == 1


def test_default_coordinator_operation_survives_health_and_replay(host, executor_results):
    # 与原coordinator回归同样读取已登记task；不以workflow/run字符串代签core身份。
    with host.tools.operation_store._repo._runtime_connection() as conn:
        row = conn.execute('SELECT tr.task_id FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
            'WHERE ar.run_id=?', (host._current_run_params.run_id,)).fetchone()
    assert row is not None and row['task_id']
    host._current_run_params = replace(host._current_run_params, task_id=row['task_id'])
    counter = CounterTool()
    counter.runtime_policy = replace(counter.runtime_policy,
        effect_resolver=EffectResolverPolicy(default_effect='mutating'),
        approval_policy=ApprovalPolicy(mode='never'), idempotency_policy=IdempotencyPolicy(scope='operation'))
    # 最低业务叶只计数并返回无身份Outcome；真实coordinator领取/结算后才产生operation。
    host.tools.register(counter)
    host._current_run_params.allowed_tools.append('native_counter')
    definition = flow('identity-coordinated', [{'step_id': 'count', 'name': 'count', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    result, run = local_run(host, definition)
    assert result.ok and run.status == 'succeeded', run.to_dict()
    [(call, original)] = executor_results
    assert original.ok and original.handler_executed and original.operation is not None
    operation = original.operation
    assert operation.operation_id
    durable_operation = host.tools.operation_store.get_tool_operation(owner_id=run.owner_id,
        run_id=call.run_id, operation_id=operation.operation_id, task_id=row['task_id'],
        attempt_id=call.attempt_id, tool_name='native_counter')
    assert durable_operation is not None and durable_operation.status == 'succeeded'
    assert durable_operation.operation_id == operation.operation_id
    step = run.steps[0]
    receipt = step.escalation_result['tool_receipt']
    assert receipt['operation_id'] == operation.operation_id
    assert step.output_snapshot['operation'] == operation.to_dict()
    assert receipt['effect_outcome'] == original.effect_outcome
    assert step.effect_id and call.idempotency_key == f'workflow-step:{run.workflow_run_id}:count'
    assert summarize_run_step_health(run)['steps'][0]['operation_id'] == operation.operation_id
    store = WorkflowRunStore(host.home_paths.owner_home_dir)
    assert build_workflow_health_view(store, workflow_id='identity-coordinated', limit=10)[
        'canonical_references']['operation_ids'] == [operation.operation_id]
    assert counter.calls == [{'value': 7}]
    again = host.tools.tools['workflows'].execute({'action': 'workflows.run',
        'workflow_ref': 'identity-coordinated', 'idempotency_key': 'native-run-0000001', 'inputs': {}})
    assert again.ok
    saved = store.load_run(run.workflow_run_id)
    assert saved.revision == run.revision
    assert saved.steps[0].escalation_result['tool_receipt'] == receipt
    assert saved.steps[0].output_snapshot == step.output_snapshot
    assert len(executor_results) == 1 and counter.calls == [{'value': 7}]


def test_native_pre_executor_denial_has_no_operation(host):
    tool = ReceiptTool('typed-success', '')
    host.tools.register(tool)
    definition = flow('identity-denied', [{'step_id': 'call', 'name': 'call', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'identity_receipt'}}])
    _, run = local_run(host, definition)
    step = run.steps[0]
    assert run.status == 'failed' and step.error_code == 'TOOL_NOT_ALLOWED'
    assert tool.calls == 0
    assert step.escalation_result['tool_receipt']['effect_outcome'] == 'not_started'
    assert step.escalation_result['tool_receipt']['operation_id'] == ''
    assert step.effect_id
    assert summarize_run_step_health(run)['steps'][0]['operation_id'] == ''


def test_missing_operation_does_not_break_resume_or_reexecute_confirmed_step(tmp_path):
    registry = ToolRegistry(ToolRegistryParams(workspace_root=tmp_path, max_chars=1000,
        max_entries=10, max_matches=10, web_max_chars=1000, http_timeout=1,
        catalog_limit=10, retrieval_limit=1, vector_search_enabled=False))
    tool = ReceiptTool('sequence', '')
    registry.register(tool)
    engine = WorkflowEngine(tmp_path)
    engine.tool_registry = registry
    definition = WorkflowDefinition(AssetDescriptor(id='identity-resume', kind='workflow',
        name='身份恢复', owner_id='unit-owner'), tuple(WorkflowStepSpec(step_id=sid, name=sid,
        mode='tool_call', on_error='fail', tool_spec={'tool_name': 'identity_receipt'})
        for sid in ('first', 'second')))
    WorkflowStore(tmp_path).save_workflow(definition)
    try:
        failed = engine.run_workflow(definition)
        assert failed.status == 'failed' and tool.calls == 2
        first = failed.steps[0].to_dict()
        assert first['status'] == 'succeeded'
        assert first['escalation_result']['tool_receipt']['operation_id'] == ''
        resumed = engine.resume_workflow_run(failed.workflow_run_id, from_step_id='second')
        assert resumed.status == 'succeeded' and tool.calls == 3, resumed.to_dict()
        assert resumed.steps[0].to_dict() == first
        assert all(s.escalation_result['tool_receipt']['operation_id'] == '' for s in resumed.steps)
        assert build_workflow_health_view(engine.store, workflow_id='identity-resume', limit=10)[
            'canonical_references']['operation_ids'] == []
    finally:
        registry.close_mcp_clients()
