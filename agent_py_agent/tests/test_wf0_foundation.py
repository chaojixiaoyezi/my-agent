# LLM: W2-WF0 轻量共享工作流底座规范测试，验证扩展字段、DAG环路检测、持久化水位与插件化引擎钩子。
# 模块用途: 为 W2-WF1..WF5 提供坚实的底座契约保证，覆盖 step schema, DAG validation, watermarks, pluggable hooks。
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs, IOContract
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowForeachAgentTaskUnsupported,
    WorkflowStepSpec,
    WorkflowStore,
    normalize_workflow_asset_id,
    validate_step_eligibility,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import (
    StepExecutionRecord,
    WorkflowRunRecord,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def _descriptor(workflow_id: str, name: str = '测试工作流') -> AssetDescriptor:
    return AssetDescriptor(
        id=workflow_id,
        kind='workflow',
        name=name,
        summary='测试工作流摘要',
        version='1.0.0',
        owner_id='owner_test',
        access_targets=(),
        data_sources=(),
        credential_refs=(),
        io_contract=IOContract(),
        refs=AssetRefs(),
    )


# =========================================================================
# 1. WorkflowStepSpec 扩展字段与验证
# =========================================================================

def test_step_spec_defaults_and_roundtrip():
    step = WorkflowStepSpec(
        step_id='s1',
        name='步骤一',
        mode='deterministic_script',
        script_id='test_script',
    )
    assert step.retry_policy is None
    assert step.depends_on == ()
    assert step.tool_spec is None
    assert step.subworkflow_ref == ''
    assert step.notification_spec is None

    as_dict = step.to_dict()
    assert 'retry_policy' not in as_dict
    assert 'depends_on' not in as_dict
    assert 'tool_spec' not in as_dict
    assert 'subworkflow_ref' not in as_dict
    assert 'notification_spec' not in as_dict

    restored = WorkflowStepSpec.from_dict(as_dict)
    assert restored.step_id == 's1'
    assert restored.retry_policy is None
    assert restored.depends_on == ()


def test_step_spec_extensions_roundtrip():
    policy = {
        'max_attempts': 3,
        'delay_seconds': 1.5,
        'backoff_factor': 2.0,
        'retryable_codes': ['TIMEOUT', 'NETWORK_ERROR'],
    }
    tool = {
        'tool_name': 'bash',
        'arguments_map': {'command': '$inputs.cmd'},
        'idempotent': True,
    }
    notice = {'channel': 'webhook', 'target': 'https://example.com/hook'}

    step = WorkflowStepSpec(
        step_id='s2',
        name='步骤二',
        mode='tool_call',
        retry_policy=policy,
        depends_on=('s1',),
        tool_spec=tool,
        subworkflow_ref='workflow:sub_flow',
        notification_spec=notice,
    )
    assert step.retry_policy == policy
    assert step.depends_on == ('s1',)
    assert step.tool_spec == tool
    assert step.subworkflow_ref == 'workflow:sub_flow'
    assert step.notification_spec == notice

    d = step.to_dict()
    assert d['retry_policy'] == policy
    assert d['depends_on'] == ['s1']
    assert d['tool_spec'] == tool
    assert d['subworkflow_ref'] == 'workflow:sub_flow'
    assert d['notification_spec'] == notice

    restored = WorkflowStepSpec.from_dict(d)
    assert restored.retry_policy == policy
    assert restored.depends_on == ('s1',)
    assert restored.tool_spec == tool
    assert restored.subworkflow_ref == 'workflow:sub_flow'
    assert restored.notification_spec == notice


def test_step_spec_retry_policy_validation():
    # max_attempts < 1
    with pytest.raises(ValueError, match='max_attempts'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy={'max_attempts': 0})

    # max_attempts is bool
    with pytest.raises(ValueError, match='max_attempts'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy={'max_attempts': True})

    # delay_seconds < 0
    with pytest.raises(ValueError, match='delay_seconds'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy={'delay_seconds': -1})

    # backoff_factor < 1.0
    with pytest.raises(ValueError, match='backoff_factor'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy={'backoff_factor': 0.5})

    # retryable_codes not strings
    with pytest.raises(ValueError, match='retryable_codes'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy={'retryable_codes': [123]})

    # non-dict retry_policy
    with pytest.raises(ValueError, match='retry_policy 必须是字典'):
        WorkflowStepSpec('s', 'n', 'script', retry_policy='invalid')


def test_step_spec_self_dependency_rejected():
    with pytest.raises(ValueError, match='不能依赖自身'):
        WorkflowStepSpec('s1', 'n', 'script', depends_on=('s1',))


# =========================================================================
# 2. WorkflowDefinition DAG 验证与环路检测
# =========================================================================

def test_workflow_definition_unknown_dependency_rejected():
    step1 = WorkflowStepSpec('s1', 'n1', 'deterministic_script', script_id='sc1')
    step2 = WorkflowStepSpec('s2', 'n2', 'deterministic_script', script_id='sc2', depends_on=('unknown_step',))
    with pytest.raises(ValueError, match='依赖未知步骤'):
        WorkflowDefinition(_descriptor('wf_err'), (step1, step2))


def test_workflow_definition_cycle_rejection():
    # 2-step cycle: s1 -> s2 -> s1
    s1 = WorkflowStepSpec('s1', 'n1', 'deterministic_script', script_id='sc1', depends_on=('s2',))
    s2 = WorkflowStepSpec('s2', 'n2', 'deterministic_script', script_id='sc2', depends_on=('s1',))
    with pytest.raises(ValueError, match='环路'):
        WorkflowDefinition(_descriptor('wf_c2'), (s1, s2))

    # 3-step cycle: a -> b -> c -> a
    sa = WorkflowStepSpec('sa', 'na', 'deterministic_script', script_id='sc1', depends_on=('sc',))
    sb = WorkflowStepSpec('sb', 'nb', 'deterministic_script', script_id='sc2', depends_on=('sa',))
    sc = WorkflowStepSpec('sc', 'nc', 'deterministic_script', script_id='sc3', depends_on=('sb',))
    with pytest.raises(ValueError, match='环路'):
        WorkflowDefinition(_descriptor('wf_c3'), (sa, sb, sc))


def test_workflow_definition_valid_dag_diamond():
    # D depends on B, C; B depends on A; C depends on A
    sa = WorkflowStepSpec('sa', 'na', 'deterministic_script', script_id='sc1')
    sb = WorkflowStepSpec('sb', 'nb', 'deterministic_script', script_id='sc2', depends_on=('sa',))
    sc = WorkflowStepSpec('sc', 'nc', 'deterministic_script', script_id='sc3', depends_on=('sa',))
    sd = WorkflowStepSpec('sd', 'nd', 'deterministic_script', script_id='sc4', depends_on=('sb', 'sc'))
    wf = WorkflowDefinition(_descriptor('wf_diamond'), (sa, sb, sc, sd))
    assert len(wf.steps) == 4


# =========================================================================
# 3. normalize_workflow_asset_id 与 WorkflowStore 寻址 (BIZ-005)
# =========================================================================

def test_normalize_workflow_asset_id():
    assert normalize_workflow_asset_id('workflow:flow_1') == 'flow_1'
    assert normalize_workflow_asset_id('flow_1') == 'flow_1'
    assert normalize_workflow_asset_id('  workflow:my_wf  ') == 'my_wf'
    assert normalize_workflow_asset_id(123) == 123


def test_workflow_store_get_by_qualified_ref(tmp_path):
    store = WorkflowStore(tmp_path)
    step = WorkflowStepSpec('s1', '步骤', 'deterministic_script', script_id='sc1')
    definition = WorkflowDefinition(_descriptor('wf_target'), (step,))
    store.save_workflow(definition)

    # 传统裸 ID 查找
    bare = store.get_workflow('wf_target')
    assert bare is not None
    assert bare.descriptor.id == 'wf_target'

    # 带 workflow: 前缀的 qualified ref 查找 (BIZ-005)
    qualified = store.get_workflow('workflow:wf_target')
    assert qualified is not None
    assert qualified.descriptor.id == 'wf_target'


# =========================================================================
# 4. models.py 扩展字段与序列化
# =========================================================================

def test_models_step_and_run_record_extensions():
    step = StepExecutionRecord(
        step_id='step_1',
        name='第一步',
        mode='tool_call',
        status='succeeded',
        started_at=100.0,
        ended_at=101.5,
        duration_ms=1500.0,
        llm_involved=False,
        attempt_count=2,
        max_attempts=3,
        subworkflow_run_id='sub_run_99',
        watermark={'offset': 10042},
        effect_id='eff_123',
    )
    d = step.to_dict()
    assert d['attempt_count'] == 2
    assert d['max_attempts'] == 3
    assert d['subworkflow_run_id'] == 'sub_run_99'
    assert d['watermark'] == {'offset': 10042}
    assert d['effect_id'] == 'eff_123'

    restored_step = StepExecutionRecord.from_dict(d)
    assert restored_step.attempt_count == 2
    assert restored_step.max_attempts == 3
    assert restored_step.subworkflow_run_id == 'sub_run_99'
    assert restored_step.watermark == {'offset': 10042}
    assert restored_step.effect_id == 'eff_123'

    run = WorkflowRunRecord(
        workflow_run_id='run_1',
        workflow_id='wf_1',
        owner_id='owner_1',
        trigger_kind='manual',
        status='running',
        started_at=100.0,
        ended_at=0.0,
        duration_ms=0.0,
        steps=(step,),
        parent_workflow_run_id='parent_run_0',
        subworkflow_depth=2,
        failure_notification={'notified': True, 'channel': 'slack'},
    )
    rd = run.to_dict()
    assert rd['parent_workflow_run_id'] == 'parent_run_0'
    assert rd['subworkflow_depth'] == 2
    assert rd['failure_notification'] == {'notified': True, 'channel': 'slack'}

    restored_run = WorkflowRunRecord.from_dict(rd)
    assert restored_run.parent_workflow_run_id == 'parent_run_0'
    assert restored_run.subworkflow_depth == 2
    assert restored_run.failure_notification == {'notified': True, 'channel': 'slack'}


# =========================================================================
# 5. run_store.py 水位持久化 (WF-10)
# =========================================================================

def test_workflow_run_store_step_watermarks(tmp_path):
    store = WorkflowRunStore(tmp_path)
    step1 = StepExecutionRecord(
        step_id='step_1',
        name='步骤一',
        mode='deterministic_script',
        status='pending',
        started_at=0.0,
        ended_at=0.0,
        duration_ms=0.0,
        llm_involved=False,
    )
    run = WorkflowRunRecord(
        workflow_run_id='run_wm',
        workflow_id='wf_wm',
        owner_id='owner_test',
        trigger_kind='manual',
        status='running',
        started_at=time.time(),
        ended_at=0.0,
        duration_ms=0.0,
        steps=(step1,),
    )
    store.create_run(run)

    # 初始状态无水位
    assert store.load_step_watermark('run_wm', 'step_1') is None

    # 保存水位
    wm_data = {'cursor': 'cur_98765', 'processed_count': 500}
    updated = store.save_step_watermark('run_wm', 'step_1', wm_data)
    assert updated.watermark == wm_data

    # 重新加载确认持久化
    loaded_wm = store.load_step_watermark('run_wm', 'step_1')
    assert loaded_wm == wm_data

    # 验证整次运行也被更新
    reloaded_run = store.load_run('run_wm')
    assert reloaded_run.steps[0].watermark == wm_data

    # 非法入参拒绝
    with pytest.raises(TypeError, match='watermark 必须是字典'):
        store.save_step_watermark('run_wm', 'step_1', 'not_a_dict')

    with pytest.raises(ValueError, match='不存在该步骤'):
        store.save_step_watermark('run_wm', 'nonexistent_step', wm_data)


# =========================================================================
# 6. WorkflowEngine 插件化步骤与生命周期钩子
# =========================================================================

def test_engine_formal_tool_call_hook(tmp_path):
    called = {}

    def mock_tool_step_executor(engine, run, workflow, step, spec, context, outputs):
        called['step_id'] = step.step_id
        called['tool_name'] = spec.get('tool_spec', {}).get('tool_name')
        step.status = 'succeeded'
        step.output_snapshot = {'result': 'tool_success'}

    engine = WorkflowEngine(tmp_path, tool_step_executor=mock_tool_step_executor)

    tool_spec = {'tool_name': 'execute_sql', 'arguments_map': {'query': 'SELECT 1'}}
    step = WorkflowStepSpec('t1', '工具步骤', 'tool_call', tool_spec=tool_spec)
    wf = WorkflowDefinition(_descriptor('wf_tool'), (step,))
    WorkflowStore(tmp_path).save_workflow(wf)

    res = engine.run_workflow(wf)
    assert res.status == 'succeeded'
    assert called['step_id'] == 't1'
    assert called['tool_name'] == 'execute_sql'
    assert res.steps[0].status == 'succeeded'
    assert res.steps[0].output_snapshot == {'result': 'tool_success'}


def test_engine_formal_notification_and_unattended_failure_hook(tmp_path):
    notified = {}
    unattended = {}

    def mock_notification_executor(engine, run, workflow, step, spec, context, outputs):
        notified['channel'] = spec.get('notification_spec', {}).get('channel')
        step.status = 'succeeded'
        step.output_snapshot = {'sent': True}

    def mock_unattended_notifier(engine, run):
        unattended['run_id'] = run.workflow_run_id
        return {'notified': True, 'channel': 'slack_alerts'}

    engine = WorkflowEngine(
        tmp_path,
        notification_step_executor=mock_notification_executor,
        unattended_failure_notifier=mock_unattended_notifier,
    )

    # 1. 成功通知步骤执行
    n_spec = {'channel': 'slack', 'target': '#ops', 'template': 'Hello'}
    step = WorkflowStepSpec('n1', '通知步骤', 'notification', notification_spec=n_spec)
    wf = WorkflowDefinition(_descriptor('wf_notice'), (step,))
    WorkflowStore(tmp_path).save_workflow(wf)

    res = engine.run_workflow(wf)
    assert res.status == 'succeeded'
    assert notified['channel'] == 'slack'
    assert unattended == {}

    # 2. 正式运行失败触发无人值守失败通知 (WF-05)
    def failing_tool_executor(engine, run, workflow, step, spec, context, outputs):
        step.status = 'failed'
        step.error_code = 'DB_UNAVAILABLE'
        step.needs_repair = True

    engine_fail = WorkflowEngine(
        tmp_path,
        tool_step_executor=failing_tool_executor,
        unattended_failure_notifier=mock_unattended_notifier,
    )
    fail_step = WorkflowStepSpec('t_fail', '失败工具步骤', 'tool_call')
    wf_fail = WorkflowDefinition(_descriptor('wf_fail'), (fail_step,))
    WorkflowStore(tmp_path).save_workflow(wf_fail)

    fail_res = engine_fail.run_workflow(wf_fail)
    assert fail_res.status == 'failed'
    assert unattended['run_id'] == fail_res.workflow_run_id
    assert fail_res.failure_notification == {'notified': True, 'channel': 'slack_alerts'}


def test_engine_formal_subworkflow_hook(tmp_path):
    called = {}

    def mock_subworkflow_executor(engine, run, workflow, step, spec, context, outputs):
        called['child_ref'] = spec.get('subworkflow_ref')
        step.status = 'succeeded'
        step.subworkflow_run_id = 'child_run_123'
        step.output_snapshot = {'child_result': 'ok'}

    engine = WorkflowEngine(tmp_path, subworkflow_step_executor=mock_subworkflow_executor)

    step = WorkflowStepSpec('sw1', '子流程步骤', 'subworkflow', subworkflow_ref='workflow:child_wf')
    wf = WorkflowDefinition(_descriptor('wf_parent'), (step,))
    WorkflowStore(tmp_path).save_workflow(wf)

    res = engine.run_workflow(wf)
    assert res.status == 'succeeded'
    assert called['child_ref'] == 'workflow:child_wf'
    assert res.steps[0].subworkflow_run_id == 'child_run_123'
    assert res.steps[0].output_snapshot == {'child_result': 'ok'}


def test_engine_step_retry_and_watermark_hooks(tmp_path):
    retry_events = []
    watermark_events = []

    def mock_retry_runner(engine, step, spec, outputs, definition, run, execute_fn):
        retry_events.append(step.step_id)
        step.attempt_count = 2
        execute_fn()

    def mock_watermark_committer(engine, run, step, spec, outputs):
        watermark_events.append((step.step_id, step.output_snapshot))

    def mock_tool_step_executor(engine, run, workflow, step, spec, context, outputs):
        step.status = 'succeeded'
        step.output_snapshot = {'processed': 42}

    engine = WorkflowEngine(
        tmp_path,
        tool_step_executor=mock_tool_step_executor,
        step_retry_runner=mock_retry_runner,
        watermark_committer=mock_watermark_committer,
    )

    policy = {'max_attempts': 3}
    step = WorkflowStepSpec('retry_step', '重试步骤', 'tool_call', retry_policy=policy)
    wf = WorkflowDefinition(_descriptor('wf_retry'), (step,))
    WorkflowStore(tmp_path).save_workflow(wf)

    res = engine.run_workflow(wf)
    assert res.status == 'succeeded'
    assert 'retry_step' in retry_events
    assert res.steps[0].attempt_count == 2
    assert res.steps[0].max_attempts == 3
    assert len(watermark_events) == 1
    assert watermark_events[0] == ('retry_step', {'processed': 42})


def test_engine_dag_scheduler_hook(tmp_path):
    dag_called = {}

    class MockDAGScheduler:
        @staticmethod
        def should_schedule_dag(workflow):
            return True

        @staticmethod
        def execute_workflow_dag(engine, record, definition, start, execution_state):
            dag_called['executed'] = True
            record.status = 'succeeded'
            return record

    engine = WorkflowEngine(tmp_path, dag_scheduler=MockDAGScheduler)
    s1 = WorkflowStepSpec('s1', '步骤一', 'deterministic_script', script_id='sc1')
    s2 = WorkflowStepSpec('s2', '步骤二', 'deterministic_script', script_id='sc2', depends_on=('s1',))
    wf = WorkflowDefinition(_descriptor('wf_dag'), (s1, s2))
    WorkflowStore(tmp_path).save_workflow(wf)

    res = engine.run_workflow(wf)
    assert res.status == 'succeeded'
    assert dag_called['executed'] is True


def test_validate_step_eligibility_foreach_agent_task_hook(monkeypatch):
    step = WorkflowStepSpec('at1', '任务', 'agent_task', foreach={'from': 'inputs.items'})

    # 默认情况下抛出 WorkflowForeachAgentTaskUnsupported
    with pytest.raises(WorkflowForeachAgentTaskUnsupported):
        validate_step_eligibility(step)

    # 当 batching.SUPPORTS_FOREACH_AGENT_TASK == True 时通过
    from agent_py_agent.agent.workflows import batching
    monkeypatch.setattr(batching, 'SUPPORTS_FOREACH_AGENT_TASK', True, raising=False)
    validate_step_eligibility(step)
