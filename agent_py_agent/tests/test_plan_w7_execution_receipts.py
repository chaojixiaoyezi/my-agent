"""W7 的执行身份、统一 IO 合同、阶段回执及返回侧取消回归。"""
import json
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.plugin_host.gateway_assets import (
    _script_fingerprint,
    _workflow_execution_digest,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.script_runner import ScriptRunner


# LLM: 仅构造临时测试定义，不授执行权。
# 函数用途: 生成带合法缺省字段的资产描述。
def _descriptor(kind='script', asset_id='sample', **extra):
    return AssetDescriptor(asset_id, kind, asset_id, **extra)


# LLM: 通过原描述类型生成定义，IO合同仍由生产解析器验证。
# 函数用途: 组合测试工作流和输出合同。
def _workflow(*steps, contract=None):
    return {'descriptor': _descriptor('workflow', 'flow', io_contract=contract or IOContract()).to_dict(),
            'steps': list(steps)}


# LLM: 使用真实分析模式名，不给节点暗加权限或兜底输出。
# 函数用途: 构造一个可定点注入故障的分析步骤。
def _analysis(step_id='one', **extra):
    return {'step_id': step_id, 'name': step_id, 'mode': 'llm_skill_analysis', **extra}


@pytest.mark.parametrize('outputs', [[{'name': 'value'}], [1], [''], 'value', {'value': 'string'}])
# LLM: 构造与工具保存共用原规则；非法形状不得留下资产或启动业务。
# 函数用途: 检查五种坏outputs在定义阶段一致早拒。
def test_outputs_shape_is_rejected_by_constructor_and_tool_save(tmp_path, outputs):
    with pytest.raises(ValueError):
        IOContract(outputs=outputs)
    descriptor = _descriptor().to_dict()
    descriptor['io_contract'] = {'outputs': outputs}
    result = ScriptsTool(tmp_path).execute({'action': 'register', 'script_id': 'sample',
        'descriptor': descriptor, 'code': 'print("{}")'})
    assert not result.ok and result.effect_outcome == 'not_started'
    assert not (tmp_path / 'scripts/sample/script.json').exists()


# LLM: 旧磁盘定义也必须重新验证，启动计数必须为零。
# 函数用途: 防止保存和执行接受不同的IO形状。
def test_legacy_invalid_outputs_are_rejected_before_process(tmp_path, monkeypatch):
    folder = tmp_path / 'scripts/sample'
    folder.mkdir(parents=True)
    record = _descriptor().to_dict() | {'entrypoint': 'main.py', 'interpreter': 'python3',
        'timeout_seconds': 1, 'io_contract': {'outputs': [{'name': 'value'}]}}
    (folder / 'script.json').write_text(json.dumps(record))
    (folder / 'main.py').write_text('print("{}")')
    calls = []
    monkeypatch.setattr('agent_py_agent.agent.workflows.script_runner._execute', lambda *args: calls.append(args))
    result = ScriptRunner(tmp_path, use_sandbox=False).run_script('sample')
    assert not result.ok and result.exit_code is None and calls == []


# LLM: 原统计写入仍增加CAS，执行身份只跟真实定义变化。
# 函数用途: 检查脚本摘要更新不会造成指纹漂移。
def test_summary_cas_does_not_change_script_execution_identity(tmp_path):
    store = ScriptStore(tmp_path)
    before = store.save_script(_descriptor(), 'print("{}")')
    grant_before, digest_before = _script_fingerprint(tmp_path, 'sample')
    after = store.record_run_summary('sample', 'succeeded', 1)
    grant_after, digest_after = _script_fingerprint(tmp_path, 'sample')
    assert after['revision'] > before['revision']
    assert before.get('execution_definition_ref') and before['execution_definition_ref'] == after['execution_definition_ref']
    assert digest_before == digest_after and grant_before == grant_after
    changed = store.save_script(_descriptor(), 'print("{\"changed\":true}")')
    assert changed['execution_definition_ref'] != before['execution_definition_ref']


# LLM: 运行统计和展示字段排除在执行指纹外，IO变化仍必须改变身份。
# 函数用途: 区分工作流存储版本与执行语义。
def test_workflow_summary_and_display_do_not_change_execution_identity(tmp_path):
    store = WorkflowStore(tmp_path)
    before = store.save_workflow(_workflow(_analysis()))
    after = store.record_run_summary('flow', 'failed', 2, last_error='fixture')
    assert after.descriptor.revision > before.descriptor.revision
    assert _workflow_execution_digest(before.to_dict()) == _workflow_execution_digest(after.to_dict())
    display = replace(after, descriptor=replace(after.descriptor, name='展示', summary='展示', tags=('展示',)))
    assert _workflow_execution_digest(display.to_dict()) == _workflow_execution_digest(before.to_dict())
    changed = replace(after, descriptor=replace(after.descriptor, io_contract=IOContract(outputs=('required',))))
    assert _workflow_execution_digest(changed.to_dict()) != _workflow_execution_digest(before.to_dict())


# LLM: 最终合同失败不能倒改成功步骤，摘要必须取原终态原因。
# 函数用途: 核对类型错配原因保留到资产运行摘要。
def test_final_contract_failure_summary_uses_breakpoint_not_successful_step(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(_workflow(_analysis(), contract=IOContract(outputs=('value',), output_types={'value': 'integer'})))
    run = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 'wrong'}).run_workflow('flow')
    assert run.status == 'failed' and run.steps[0].status == 'succeeded'
    assert run.breakpoint['error_message'] == 'SCRIPT_OUTPUT_TYPE_MISMATCH'
    assert 'SCRIPT_OUTPUT_TYPE_MISMATCH' in store.get_workflow('flow').descriptor.runtime_summary.last_error


@pytest.mark.parametrize('error_type', [ValueError, OSError])
# LLM: 对原保存入口注入单次故障，重启引擎必须复用同一run和成功前缀。
# 函数用途: 检查ValueError和存储异常都不会被误报为未开始。
def test_step_publish_failure_retains_run_and_confirmed_prefix_on_resume(tmp_path, monkeypatch, error_type):
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, *_: calls.append(spec['step_id']) or {'value': 1})
    WorkflowStore(tmp_path).save_workflow(_workflow(_analysis(), _analysis('two')))
    original = engine.store.save_step_result
    failed = False

    # LLM: 只在第一次成功步骤发表时失败，后续仍走原持久化实现。
    # 函数用途: 制造可恢复的单次保存故障。
    def save(run_id, step):
        nonlocal failed
        if step.status == 'succeeded' and not failed:
            failed = True
            raise error_type('single publish failure')
        return original(run_id, step)

    monkeypatch.setattr(engine.store, 'save_step_result', save)
    result = WorkflowsTool(tmp_path, engine=engine).execute({'action': 'run', 'workflow_id': 'flow'})
    data = json.loads(result.output)
    assert not result.ok and result.effect_outcome != 'not_started'
    assert data.get('workflow_run_id') and data.get('phase') == 'step_publish'
    assert data.get('confirmed_step_refs') == ['one'] and calls == ['one']
    restarted = WorkflowEngine(tmp_path, analysis_handler=lambda spec, *_: calls.append(spec['step_id']) or {'value': 2})
    resumed = restarted.resume_workflow_run(data['workflow_run_id'])
    assert resumed.status == 'succeeded' and calls == ['one', 'two']


# LLM: 缺失必需输入不能用空对象伪装成读到了材料。
# 函数用途: 核对输入未找到时零次分析调用。
def test_analysis_missing_input_is_not_silently_empty(tmp_path):
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(args) or {})
    run = engine.run_workflow(_workflow(_analysis(inputs_map={'required': {'step_id': '$inputs', 'key': 'missing'}})))
    assert run.status == 'failed' and not calls
    assert run.steps[0].error_code == 'STEP_EXECUTION_ERROR'


# LLM: Event在分析返回前置位，输出边界必须消费同一取消事实。
# 函数用途: 证明已接受取消后不发表步骤或运行成功。
def test_return_side_cancellation_does_not_publish_success(tmp_path):
    returned = Event()
    token = CancellationToken(_event=returned)

    # LLM: 仅置同一宿主取消事件，不另建控制状态。
    # 函数用途: 模拟模型返回时取消已被接受。
    def analyze(*args):
        returned.set()
        return {'value': 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        engine.run_workflow(_workflow(_analysis()))
    records = engine.store.list_runs()
    assert len(records) == 1 and records[0].status != 'succeeded'
    assert records[0].steps[0].status != 'succeeded'


# LLM: 原运行的增量字段必须跨replace和原Store写入保留。
# 函数用途: 核对步骤发表不会擦掉定义引用和实际阶段。
def test_step_publish_preserves_definition_and_actual_receipt(tmp_path):
    from agent_py_agent.agent.workflows.run_store import execution_receipt

    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    run = engine.run_workflow(_workflow(_analysis()))
    definition_ref = run.execution_definition_ref
    execution_receipt(run, phase='started', effect_outcome='unknown')
    engine.store.save_run(run)
    engine.store.save_step_result(run.workflow_run_id, run.steps[0])
    reloaded = engine.store.load_run(run.workflow_run_id)
    assert reloaded.execution_definition_ref == definition_ref
    assert reloaded.phase == 'step_publish' and reloaded.effect_outcome == 'unknown'
    assert reloaded.confirmed_step_refs == ('one',)


# LLM: 已通过业务输出边界的成功步骤不可被晚到取消改成可重跑。
# 函数用途: 用重启后的调用计数证明前缀保留且后继正常续跑。
def test_cancel_after_confirmed_step_preserves_prefix_and_does_not_repeat(tmp_path, monkeypatch):
    token = CancellationToken()
    calls = []

    # LLM: 仅记录真实进入分析器的步骤名，不模拟成功前缀读取。
    # 函数用途: 收集原运行和恢复后的实际执行顺序。
    def handler(spec, *_):
        calls.append(spec['step_id'])
        return {'value': 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=handler)
    perform = engine._perform

    # LLM: 先走真实步骤再取消，区别返回前取消与确认后取消。
    # 函数用途: 定点触发成功步骤后的晚到停止。
    def cancel_after_success(*args, **kwargs):
        perform(*args, **kwargs)
        token.cancel('after step output confirmation')

    monkeypatch.setattr(engine, '_perform', cancel_after_success)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled) as caught:
        engine.run_workflow(_workflow(_analysis(), _analysis('two')))
    run = engine.store.list_runs()[0]
    assert run.status == 'paused_for_resume' and run.steps[0].status == 'succeeded'
    assert run.confirmed_step_refs == ('one',)
    assert caught.value.receipt['workflow_run_id'] == run.workflow_run_id
    restarted = WorkflowEngine(tmp_path, analysis_handler=handler)
    result = restarted.resume_workflow_run(run.workflow_run_id)
    assert result.status == 'succeeded' and calls == ['one', 'two']


@pytest.mark.parametrize('boundary', ['finalize', 'terminal_write'])
# LLM: 覆盖最终合同之后及原run写入之前的安全点，不外推未测试的原子rename竞态。
# 函数用途: 检查终态停止保留原ID、实际阶段和成功前缀。
def test_cancel_at_final_publish_retains_original_receipt(tmp_path, monkeypatch, boundary):
    token = CancellationToken()
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    finalize, save, write_step = engine._finalize_record, engine.store.save_run, engine.store._write_step
    in_terminal_save = False

    # LLM: 原结算逻辑仍执行，只在成功候选形成后置取消。
    # 函数用途: 制造最终结算后停止。
    def finalize_then_cancel(*args):
        result = finalize(*args)
        if boundary == 'finalize' and result.status == 'succeeded':
            token.cancel('after finalization')
        return result

    # LLM: 只标记原终态保存作用域，不替换真实磁盘写入。
    # 函数用途: 给步骤快照探针区分普通发表和终态发表。
    def save_terminal(record, *args, **kwargs):
        nonlocal in_terminal_save
        in_terminal_save = record.status == 'succeeded'
        try:
            return save(record, *args, **kwargs)
        finally:
            in_terminal_save = False

    # LLM: 最后一步快照写完后取消，必须在run成功发表前再次检查。
    # 函数用途: 制造终态存储内部的停止窗口。
    def write_then_cancel(*args):
        result = write_step(*args)
        if boundary == 'terminal_write' and in_terminal_save:
            token.cancel('before run terminal publication')
        return result

    monkeypatch.setattr(engine, '_finalize_record', finalize_then_cancel)
    monkeypatch.setattr(engine.store, 'save_run', save_terminal)
    monkeypatch.setattr(engine.store, '_write_step', write_then_cancel)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled) as caught:
        engine.run_workflow(_workflow(_analysis()))
    run = engine.store.list_runs()[0]
    assert run.status == 'paused_for_resume' and run.steps[0].status == 'succeeded'
    assert caught.value.receipt['workflow_run_id'] == run.workflow_run_id
    assert caught.value.receipt['phase'] in ('finalize', 'terminal')
    assert caught.value.receipt['effect_outcome'] != 'not_started'
    assert caught.value.receipt['confirmed_step_refs'] == ('one',)


# LLM: 附属摘要故障不得改变原Store已经确认的成功事实。
# 函数用途: 检查提交后异常不会倒改终态。
def test_summary_error_after_commit_cannot_rewrite_success(tmp_path, monkeypatch):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})

    # LLM: 只注入提交后的附属故障，不改运行数据。
    # 函数用途: 模拟摘要存储不可用。
    def fail_summary(*args):
        raise OSError('summary store unavailable after commit')

    monkeypatch.setattr(engine, '_record_summary', fail_summary)
    WorkflowStore(tmp_path).save_workflow(_workflow(_analysis()))
    result = WorkflowsTool(tmp_path, engine=engine).execute({'action': 'run', 'workflow_id': 'flow'})
    run = engine.store.list_runs()[0]
    assert result.ok and run.status == 'succeeded'
    assert run.effect_outcome == 'confirmed' and run.confirmed_step_refs == ('one',)


@pytest.mark.parametrize('poison', ['error_code', 'output'])
# LLM: 坏输出或坏异常元数据不能使错误包装再次失败而丢掉原run。
# 函数用途: 核对序列化失败仍保留已确认前缀和真实开始事实。
def test_unserializable_step_never_masks_started_run_receipt(tmp_path, monkeypatch, poison):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    perform = engine._perform

    # LLM: 首步保持原执行，仅在后继节点注入不可序列化值。
    # 函数用途: 分别制造异常字段和输出快照的JSON故障。
    def poisoned_step(step, spec, *args, **kwargs):
        if spec['step_id'] == 'two':
            if poison == 'error_code':
                error = ValueError('bad error metadata')
                error.error_code = float('nan')
                raise error
            step.output_snapshot = {'value': float('nan')}
            step.status = 'succeeded'
            return
        return perform(step, spec, *args, **kwargs)

    monkeypatch.setattr(engine, '_perform', poisoned_step)
    WorkflowStore(tmp_path).save_workflow(_workflow(_analysis(), _analysis('two')))
    result = WorkflowsTool(tmp_path, engine=engine).execute({'action': 'run', 'workflow_id': 'flow'})
    data = json.loads(result.output)
    assert not result.ok and result.effect_outcome != 'not_started'
    assert data.get('workflow_run_id') and data.get('phase') == 'step_publish'
    assert data.get('confirmed_step_refs') == ['one']


@pytest.mark.parametrize('readback_fails', [False, True])
# LLM: ACK异常不等于未提交；回读不可用时只能unknown，不能覆盖可能已提交历史。
# 函数用途: 检查终态写入后异常的可确认与未知两条收口路径。
def test_terminal_write_ack_error_does_not_undo_observable_committed_success(tmp_path, monkeypatch, readback_fails):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    save, load = engine.store.save_run, engine.store.load_run
    written, probed = False, False

    # LLM: 先完成原原子写再抛错，禁止把该异常误当成写入前失败。
    # 函数用途: 模拟终态提交成功但确认返回丢失。
    def fail_after_commit(record, *args, **kwargs):
        nonlocal written
        result = save(record, *args, **kwargs)
        if record.status == 'succeeded':
            written = True
            raise OSError('lost acknowledgement after atomic write')
        return result

    # LLM: 仅一次原提交观察失败，后续核验仍读原Store。
    # 函数用途: 覆盖提交结果暂时无法核实的情况。
    def load_after_commit(*args):
        nonlocal probed
        if readback_fails and written and not probed:
            probed = True
            raise OSError('commit observation unavailable')
        return load(*args)

    monkeypatch.setattr(engine.store, 'save_run', fail_after_commit)
    monkeypatch.setattr(engine.store, 'load_run', load_after_commit)
    WorkflowStore(tmp_path).save_workflow(_workflow(_analysis()))
    result = WorkflowsTool(tmp_path, engine=engine).execute({'action': 'run', 'workflow_id': 'flow'})
    run = load(json.loads(result.output)['workflow_run_id'])
    assert result.ok is not readback_fails
    assert run.status == 'succeeded' and run.effect_outcome == 'confirmed'
    if readback_fails:
        assert result.effect_outcome == 'unknown'
    assert run.confirmed_step_refs == ('one',)
