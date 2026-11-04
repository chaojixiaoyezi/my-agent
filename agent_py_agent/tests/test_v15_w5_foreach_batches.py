"""批处理公开入口、真实账本及确定性交错回归。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec, WorkflowStore
from agent_py_agent.agent.workflows import batching
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord
from agent_py_agent.tests.test_workflow_script_engine import workflow


# LLM: 只使用原公开定义入口，临时目录由pytest隔离。
# 函数用途: 建立一个按初始输入切批的分析工作流。
def batch_flow(*, policy='fail_fast', mode='map', parallel=1, size=2):
    assert 'foreach' in WorkflowStepSpec.__dataclass_fields__, 'foreach字段尚未实现'
    return workflow({'step_id': 'judge', 'name': 'judge', 'mode': 'llm_skill_analysis', 'on_error': 'fail',
                     'foreach': {'from': 'inputs.rows', 'batch_size': size, 'max_parallel': parallel,
                                 'on_batch_error': policy, 'mode': mode, 'as': 'batch'}})


# LLM: 表格覆盖类型混淆、边界及未知字段，不放宽保存合同。
# 函数用途: 锁住foreach校验错误码。
@pytest.mark.parametrize('config', [None, {}, {'from': 'rows'}, {'from': 'inputs.rows', 'batch_size': 0},
    {'from': 'inputs.rows', 'batch_size': 501}, {'from': 'inputs.rows', 'batch_size': True},
    {'from': 'inputs.rows', 'max_parallel': 9}, {'from': 'inputs.rows', 'max_parallel': False},
    {'from': 'inputs.rows', 'as': 'bad-name'}, {'from': 'inputs.rows', 'as': 'class'},
    {'from': 'inputs.rows', 'max_batch_tokens': 0}, {'from': 'inputs.rows', 'mode': 'other'},
    {'from': 'inputs.rows', 'on_batch_error': 'other'}, {'from': 'inputs.rows', 'unknown': 1}])
def test_foreach_invalid_table(config):
    with pytest.raises(ValueError, match='WORKFLOW_FOREACH_INVALID'):
        batching.validate_foreach(config)


# LLM: 保存再读真实资产，旧定义的默认字段不得改变原字节形状。
# 函数用途: 验证声明往返、默认值和旧记录兼容。
def test_foreach_save_roundtrip_and_old_shape(tmp_path):
    value = batch_flow()
    saved = WorkflowStore(tmp_path).save_workflow(value)
    assert WorkflowStore(tmp_path).get_workflow('daily') == saved
    assert saved.steps[0].foreach == value['steps'][0]['foreach']
    old = WorkflowStepSpec('old', 'old', 'llm_skill_analysis').to_dict()
    assert 'foreach' not in old
    record = StepExecutionRecord('old', 'old', 'llm_skill_analysis', 'pending', 0, 0, 0, False)
    assert record.batches == [] and record.batches_summary == {}
    assert StepExecutionRecord.from_dict(record.to_dict()) == record


# LLM: JSON字节估算必须包含UTF8与数组分隔开销，输入顺序不可改变。
# 函数用途: 验证确定性贪心切批与单条超预算标记。
def test_token_packing_order_count_and_oversize():
    items = ['a', 'b', '中' * 20, 'c', 'd']
    result = batching.pack_items(items, batch_size=2, max_batch_tokens=3)
    assert [batch['items'] for batch in result] == [['a', 'b'], ['中' * 20], ['c', 'd']]
    assert [batch['oversize'] for batch in result] == [False, True, False]
    assert result == batching.pack_items(items, batch_size=2, max_batch_tokens=3)
    assert [(b['start'], b['end']) for b in result] == [(0, 2), (2, 3), (3, 5)]


# LLM: 回调只替代外部模型，运行、账本和恢复均使用真实引擎。
# 函数用途: 验证40条在单run中产生5个有序批和逐批持久化。
def test_one_run_batches_persist_each_completion(tmp_path):
    seen, durable = [], []
    engine = WorkflowEngine(tmp_path)

    # LLM: 读取原运行快照而非伪造保存结果。
    # 函数用途: 检查下一批开始前前批已落盘。
    def analyze(spec, context, skills):
        rows = context.step_inputs['batch']
        seen.append(rows)
        runs = list((tmp_path / 'data/workflow_runs').iterdir())
        persisted = engine.store.load_run(runs[0].name).steps[0]
        durable.append(persisted.batches_summary['completed'])
        return {'values': rows}

    engine.analysis_handler = analyze
    run = engine.run_workflow(batch_flow(size=8), {'rows': list(range(40))})
    assert run.status == 'succeeded' and len(seen) == 5
    assert durable == list(range(5))
    assert run.final_output == [{'values': list(range(i, i + 8))} for i in range(0, 40, 8)]
    assert run.steps[0].batches_summary == {'total': 5, 'completed': 5, 'failed': 0, 'blocked': 0, 'skipped': 0}
    assert all(row['status'] == 'succeeded' and row['item_count'] == 8 for row in run.steps[0].batches)
    assert engine.store.load_run(run.workflow_run_id) == run


# LLM: 不通过改断言消除失败；失败批重跑但已完成批不再调用模型。
# 函数用途: 验证两种失败策略和EC49恢复。
@pytest.mark.parametrize('policy,first_calls', [('fail_fast', [0, 2]), ('continue', [0, 2, 4])])
def test_batch_error_policy_and_resume_completed_prefix(tmp_path, policy, first_calls):
    calls = []
    failing = [True]

    # LLM: 外部分析器在第二批模拟业务异常，其余输出不变。
    # 函数用途: 为批错误策略提供可证伪输入。
    def analyze(spec, context, skills):
        rows = context.step_inputs['batch']
        calls.append(rows[0])
        if rows[0] == 2 and failing[0]:
            raise ValueError('simulated batch failure')
        return {'values': rows}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    run = engine.run_workflow(batch_flow(policy=policy, mode='aggregate'), {'rows': list(range(6))})
    assert calls == first_calls
    assert run.steps[0].batches_summary['failed'] == 1
    if policy == 'fail_fast':
        assert run.status == 'failed' and run.steps[0].error_code == 'WORKFLOW_BATCH_FAILED'
        assert run.effect_outcome != 'confirmed'
        failing[0] = False
        resumed = engine.resume_workflow_run(run.workflow_run_id)
        assert resumed.status == 'succeeded' and calls == [0, 2, 2, 4]
        assert resumed.final_output == {'values': list(range(6))}
    else:
        assert run.status == 'succeeded' and run.final_output == {'values': [0, 1, 4, 5]}


# LLM: 异常来自真实账本写入口，外部模型不重复成功批。
# 函数用途: 验证发表中断后的单run恢复。
def test_simulated_io_error_preserves_completed_batches(tmp_path, monkeypatch):
    calls, triggered = [], []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        calls.append(context.step_inputs['batch'][0]) or {'values': context.step_inputs['batch']})
    original = engine.store.save_step_result

    # LLM: 第二批启动前仅注入一次I/O故障，后续保存仍走原实现。
    # 函数用途: 模拟批账本发表边界的暂时写失败。
    def save(run_id, record, *, resuming=False):
        if record.batches_summary.get('completed') == 1 and any(row['status'] == 'running' for row in record.batches) and not triggered:
            triggered.append(True)
            raise OSError('simulated I/O error')
        return original(run_id, record, resuming=resuming)

    monkeypatch.setattr(engine.store, 'save_step_result', save)
    run = engine.run_workflow(batch_flow(mode='aggregate'), {'rows': list(range(6))})
    assert triggered and run.status != 'succeeded' and calls == [0]
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded' and calls == [0, 2, 4]
    assert resumed.final_output == {'values': list(range(6))}


# LLM: Event强制两个批同时进入，每线程绑定独立上下文；不靠sleep安排时序。
# 函数用途: 验证有界池、上下文继承、完成乱序与输出顺序。
def test_parallel_pool_propagates_context_and_keeps_order(tmp_path):
    token = ContextVar('w5_run_identity')
    token.set('host-run')
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, maximum, identities = [0], [0], []

    # LLM: 仅阻塞模型替身，原引擎保留运行互斥及账本写权限。
    # 函数用途: 通过可控交错观察两个worker的真实并发。
    def analyze(spec, context, skills):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            identities.append(token.get(None))
            if active[0] == 2:
                entered.set()
        assert release.wait(10)
        with lock:
            active[0] -= 1
        return {'values': context.step_inputs['batch']}

    # LLM: 父执行线程也须显式绑定自身身份，不能误用启动线程的默认值。
    # 函数用途: 在独立宿主执行线程启动有界批池。
    def execute():
        token.set('host-run')
        return WorkflowEngine(tmp_path, analysis_handler=analyze).run_workflow(
            batch_flow(parallel=2, mode='aggregate'), {'rows': list(range(8))})

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(execute)
        try:
            assert entered.wait(8)
        finally:
            release.set()
        run = future.result(15)
    assert run.status == 'succeeded' and maximum[0] == 2 and identities == ['host-run'] * 4
    assert run.final_output == {'values': list(range(8))}


# LLM: 缺失和非数组引用不是空批成功。
# 函数用途: 验证运行时路径诊断及空列表语义。
@pytest.mark.parametrize('rows', [None, {}, 'value'])
def test_non_list_foreach_path_is_structured_failure(tmp_path, rows):
    run = WorkflowEngine(tmp_path).run_workflow(batch_flow(), {'rows': rows})
    assert run.status == 'failed' and run.steps[0].error_code == 'WORKFLOW_FOREACH_INVALID'
    assert 'inputs.rows' in run.steps[0].error_message


# LLM: 保存后的JSON仍走严格定义恢复，不直接绕过字段验证。
# 函数用途: 验证foreach不改变refs派生。
def test_refs_recompute_still_uses_step_dependencies(tmp_path):
    value = batch_flow()
    value['steps'][0]['script_id'] = 'collect'
    saved = WorkflowStore(tmp_path).save_workflow(value)
    assert saved.descriptor.refs.scripts == ('collect',)
    assert json.loads((tmp_path / 'workflows/daily/workflow.json').read_text())['steps'][0]['foreach']['from'] == 'inputs.rows'


# LLM: JSON字段名不限英文，解析只按显式路径，不按语言否定有效输入。
# 函数用途: 验证中文输出路径和保存合同。
def test_foreach_accepts_unicode_json_field_path(tmp_path):
    value = batch_flow(mode='aggregate')
    value['steps'][0]['foreach']['from'] = 'inputs.候选'
    saved = WorkflowStore(tmp_path).save_workflow(value)
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        {'values': context.step_inputs['batch']}).run_workflow(saved, {'候选': [1, 2, 3]})
    assert run.status == 'succeeded' and run.final_output == {'values': [1, 2, 3]}


# LLM: 批worker不能把原有中断合同降成普通业务失败；退出前由原引擎暂停并收口。
# 函数用途: 锁定并发批的中断传播与非成功回执。
def test_batch_worker_does_not_swallow_interruption(tmp_path):
    # LLM: 仅在模型边界发出原生中断，不改变宿主控制记录。
    # 函数用途: 模拟本批执行栈停止。
    def analyze(spec, context, skills):
        raise InterruptedError('simulated interruption')

    with pytest.raises(InterruptedError) as caught:
        WorkflowEngine(tmp_path, analysis_handler=analyze).run_workflow(batch_flow(parallel=2), {'rows': [1, 2]})
    assert caught.value.record.status == 'paused_for_resume'
    assert caught.value.record.effect_outcome != 'confirmed'
