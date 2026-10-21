# LLM: 工作流引擎公开入口回归；使用真实临时脚本与隔离账本，不访问模型或外部服务。
# 模块用途: 验证零模型快跑、异常恢复、技能分析与重启后的断点续跑。
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

from agent_py_agent.agent.workflows import ScriptRunner, WorkflowEngine, WorkflowRunStore
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord


# LLM: 测试资产协议与资产存储任务保持同形；只写 pytest 临时目录。
# 函数用途: 注册可真实执行的脚本及其数据源和输出合同。
def script(home, code='print(\'{"value": 1}\')', **options):
    folder = home / 'scripts' / options.pop('script_id', 'collect')
    folder.mkdir(parents=True, exist_ok=True)
    metadata = {'id': folder.name, 'kind': 'script', 'name': '测试采集', 'owner_id': 'alice',
                'data_sources': [{'source_id': 'apps', 'system': 'logs', 'query_scope': {'active_appnames': ['A', 'B']}}],
                'io_contract': {'outputs': ['value']}, 'entrypoint': 'main.py',
                'interpreter': 'python3', 'timeout_seconds': 2}
    metadata.update(options)
    (folder / 'script.json').write_text(json.dumps(metadata))
    (folder / metadata['entrypoint']).write_text(code)
    return folder


# LLM: 输入是公开 JSON 步骤，不依赖并行任务的模块是否已落盘。
# 函数用途: 构造一个与 WorkflowDefinition 序列化后同形的工作流。
def workflow(*steps):
    return {'descriptor': {'id': 'daily', 'owner_id': 'alice'}, 'steps': list(steps)}


# LLM: 只构造确定性步，不在测试 helper 内执行脚本。
# 函数用途: 减少案例中重复的步骤字段。
def step(step_id='collect', **options):
    return {'step_id': step_id, 'name': step_id, 'mode': 'deterministic_script',
            'script_id': 'collect', **options}


# LLM: 测试真实 stdin、环境数据源与输出合同，防止数据源被写死。
# 函数用途: 验证正常脚本没有任何模型依赖，且输入和配置确实传到子进程。
def test_script_inputs_sources_and_timing(tmp_path, monkeypatch):
    monkeypatch.setenv('HOST_API_SECRET', 'must-not-inherit')
    script(tmp_path, '''import json, os, sys
inputs = json.load(sys.stdin)
assert inputs == json.loads(os.environ['MY_AGENT_SCRIPT_INPUT_JSON'])
assert 'HOST_API_SECRET' not in os.environ
print(json.dumps({'value': inputs['number'], 'sources': json.loads(os.environ['MY_AGENT_DATA_SOURCES_JSON'])}))
''')
    result = ScriptRunner(tmp_path).run_script('collect', {'number': 7})
    assert result is not None
    assert result.ok and not result.needs_repair
    assert result.output_data['value'] == 7
    assert result.output_data['sources'][0]['query_scope'] == {'active_appnames': ['A', 'B']}
    assert result.exit_code == 0 and result.error_code == ''
    assert result.started_at <= result.ended_at and result.duration_ms >= 0


# LLM: 错误签名逐项锁住；不得把错误进程或无效输出当成功。
# 函数用途: 验证非零退出、解析错误、非对象和缺字段都会产生结构化修复上下文。
@pytest.mark.parametrize(('code', 'error'), [
    ('import sys; print("broken", file=sys.stderr); sys.exit(4)', 'SCRIPT_EXIT_NONZERO'),
    ('print("not json")', 'SCRIPT_INVALID_JSON'),
    ('print("[]")', 'SCRIPT_INVALID_JSON'),
    ('print("{}")', 'SCRIPT_MISSING_OUTPUT_KEYS'),
    ('import time; time.sleep(5)', 'SCRIPT_TIMEOUT'),
])
def test_script_failures_are_repairable(tmp_path, code, error):
    script(tmp_path, code, timeout_seconds=0.1)
    result = ScriptRunner(tmp_path).run_script('collect', {'task': 'sample'})
    assert result is not None
    assert result.ok is False and result.needs_repair is True
    assert result.error_code == error
    assert result.escalation_context['script_id'] == 'collect'
    assert result.escalation_context['inputs'] == {'task': 'sample'}
    assert result.escalation_context['data_sources'][0]['query_scope']['active_appnames'] == ['A', 'B']
    assert result.escalation_context['error_code'] == error
    assert result.escalation_context['exit_code'] == result.exit_code
    assert result.escalation_context['stderr_preview'] == result.stderr_preview


# LLM: 使用真实 sh 和 bash，不把解释器名称作为 shell 命令拼接。
# 函数用途: 验证两种 shell 固化脚本均能产生合同 JSON。
@pytest.mark.parametrize('interpreter', ['sh', 'bash'])
def test_shell_scripts(tmp_path, interpreter):
    script(tmp_path, 'printf \'{"value": 8}\\n\'\n', interpreter=interpreter, entrypoint='run.sh')
    assert ScriptRunner(tmp_path).run_script('collect').output_data == {'value': 8}


# LLM: 静默成功链路不可触碰任何 LLM 回调。
# 函数用途: 验证纯脚本顺序执行、输入映射和每步独立落盘。
def test_script_only_workflow_and_input_mapping(tmp_path):
    script(tmp_path, 'import json,sys; print(json.dumps({"value": json.load(sys.stdin)["number"]+1}))')
    definition = workflow(step(), step('second', inputs_map={'number': {'step_id': 'collect', 'key': 'value'}}))
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow(definition, {'number': 1}, owner_id='alice')
    assert run is not None
    assert run.status == 'succeeded' and run.final_output == {'value': 3}
    assert [s.status for s in run.steps] == ['succeeded', 'succeeded']
    assert not any(s.llm_involved for s in run.steps)
    root = tmp_path / 'data' / 'workflow_runs' / run.workflow_run_id
    assert json.loads((root / 'run.json').read_text())['status'] == 'succeeded'
    assert json.loads((root / 'steps' / 'collect.json').read_text())['output_snapshot'] == {'value': 2}
    assert WorkflowRunStore(tmp_path).load_run(run.workflow_run_id) == run


# LLM: 条件仅来自结构化输出，不能求值自然语言或执行表达式。
# 函数用途: 无异常时逐个跳过显式受条件约束的步骤，解析器和模型均不调用。
def test_no_anomaly_skips_remaining_without_llm(tmp_path):
    script(tmp_path, 'print(\'{"value": 0, "anomaly_detected": false}\')')
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(args),
                            escalation_handler=lambda *args: calls.append(args),
                            skill_resolver=lambda *args: calls.append(args))
    condition = {'step_id': 'collect', 'key': 'anomaly_detected', 'equals': True}
    run = engine.run_workflow(workflow(step(), step('judge', mode='llm_skill_analysis', condition=condition),
                                      step('tail', mode='llm_skill_analysis', condition=condition)))
    assert run is not None
    assert run.status == 'succeeded'
    assert [s.status for s in run.steps] == ['succeeded', 'skipped', 'skipped']
    assert not any(s.llm_involved for s in run.steps) and calls == []
    assert run.final_output['anomaly_detected'] is False


# LLM: 恢复数据与修复草案只记账，不能自动改写固化脚本。
# 函数用途: 验证脚本报错后调用兜底，并用恢复输出推进下一步。
def test_escalated_recovery_and_dynamic_skill_merge(tmp_path):
    folder = script(tmp_path, 'raise RuntimeError("upstream failed")')
    seen = []
    # LLM: 模拟宿主兜底，仅记录请求并返回数据，不修改脚本。
    # 函数用途: 提供应急输出与修复草案。
    def recover(context):
        seen.append(context)
        return {'recovered': True, 'output_data': {'value': 9}, 'repair_patch_draft': 'draft only'}
    # LLM: 模拟技能解析器，断言传入资产配置而非硬编码来源。
    # 函数用途: 返回含重复项的动态技能列表以验证去重。
    def resolve(ids, tags, sources):
        assert ids == ('static',) and tags == ('security',)
        assert sources[0]['query_scope'] == {'active_appnames': ['A', 'B']}
        return ['dynamic', 'static']
    # LLM: 只消费输入副本，校验分析器实际拿到恢复数据。
    # 函数用途: 返回结构化分析结果。
    def analyze(spec, outputs, skills):
        assert outputs == {'collect': {'value': 9}}
        assert skills == ('static', 'dynamic')
        return {'decision': 'alert'}
    engine = WorkflowEngine(tmp_path, escalation_handler=recover, skill_resolver=resolve, analysis_handler=analyze)
    run = engine.run_workflow(workflow(step(on_error='escalate_to_agent'),
                                      step('judge', mode='llm_skill_analysis', skill_ids=['static'], skill_tags=['security'])))
    assert run is not None
    assert run.status == 'succeeded' and run.final_output == {'decision': 'alert'}
    assert run.steps[0].status == 'succeeded' and run.steps[0].llm_involved
    assert run.steps[0].escalation_result['repair_patch_draft'] == 'draft only'
    assert run.steps[1].resolved_skill_ids == ('static', 'dynamic')
    assert run.steps[1].llm_involved and len(seen) == 1
    assert (folder / 'main.py').read_text() == 'raise RuntimeError("upstream failed")'


# LLM: 失败后重建引擎模拟进程重启；前步计数器不得再次增长。
# 函数用途: 验证默认失败步恢复与指定中间步恢复都会复用前序输出。
def test_resume_reuses_outputs_and_frozen_definition(tmp_path, monkeypatch):
    script(tmp_path, 'print(\'{"value": 10}\')')
    executions = []
    original = ScriptRunner.run_script
    # LLM: 记录真实执行入口，不替换执行结果或依赖资产目录作为脚本cwd。
    # 函数用途: 观察断点恢复是否再次执行成功前缀。
    def observe(runner, script_id, *args, **kwargs):
        executions.append(script_id)
        return original(runner, script_id, *args, **kwargs)
    monkeypatch.setattr(ScriptRunner, 'run_script', observe)
    script(tmp_path, 'raise RuntimeError("offline")', script_id='broken')
    definition = workflow(step(), step('broken', script_id='broken'), step('judge', mode='llm_skill_analysis'))
    run = WorkflowEngine(tmp_path).run_workflow(definition, {'number': 1})
    assert run is not None
    assert run.status == 'paused_for_resume' and run.failed_step_id == 'broken'
    definition['steps'] = []
    (tmp_path / 'scripts' / 'broken' / 'main.py').write_text('print(\'{"value": 20}\')')
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, out, skills: calls.append(out) or {'done': True})
    resumed = engine.resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded' and resumed.resumed_from_step_id == 'broken'
    assert executions == ['collect', 'broken', 'broken']
    assert resumed.steps[0] == run.steps[0]
    assert calls == [{'collect': {'value': 10}, 'broken': {'value': 20}}]
    again = engine.resume_workflow_run(run.workflow_run_id, 'judge')
    assert again.status == 'succeeded' and executions == ['collect', 'broken', 'broken']
    assert engine.resume_workflow_run(run.workflow_run_id) == again


# LLM: 默认未恢复时不可执行尾步；回调异常也必须落在失败步。
# 函数用途: 验证未配置兜底、未恢复和兜底抛错的断点状态。
@pytest.mark.parametrize(('handler', 'expected'), [(None, 'paused_for_resume'),
    (lambda ctx: {'recovered': False}, 'paused_for_resume'), (lambda ctx: 1 / 0, 'failed')])
def test_unrecovered_failures_stop_workflow(tmp_path, handler, expected):
    script(tmp_path, 'raise RuntimeError("offline")')
    run = WorkflowEngine(tmp_path, escalation_handler=handler).run_workflow(workflow(step(), step('tail')))
    assert run is not None
    assert run.status == expected and run.failed_step_id == 'collect'
    assert run.steps[0].status == expected and run.steps[1].status == 'pending'
    assert run.steps[0].llm_involved is (handler is not None)


# LLM: 公共账本 API 必须独立可用，加载结果恢复 tuple 字段并隔离 mutable 默认值。
# 函数用途: 验证创建、逐步保存、筛选、限量以及未知步骤拒绝。
def test_run_store_round_trip_filtering_and_defaults(tmp_path):
    store = WorkflowRunStore(tmp_path)
    first = WorkflowRunRecord('r1', 'daily', 'alice', 'manual', 'running', time.time(), 0, 0,
                              steps=(StepExecutionRecord('one', 'one', 'deterministic_script', 'pending', 0, 0, 0, False),))
    store.create_run(first)
    completed = replace(first.steps[0], status='succeeded', output_snapshot={'value': 3}, resolved_skill_ids=('s',))
    store.save_step_result('r1', completed)
    assert store.load_run('r1') is not None
    assert store.load_run('r1').steps == (completed,)
    second = replace(first, workflow_run_id='r2', workflow_id='other', status='failed', started_at=first.started_at+1)
    store.create_run(second)
    assert [r.workflow_run_id for r in store.list_runs(limit=1)] == ['r2']
    assert [r.workflow_run_id for r in store.list_runs(workflow_id='daily', status='running')] == ['r1']
    with pytest.raises(ValueError):
        store.create_run(first)
    with pytest.raises(ValueError):
        store.save_step_result('r1', replace(completed, step_id='unknown'))
    assert json.loads(json.dumps(asdict(first)))['steps'][0]['step_id'] == 'one'
    first.inputs['x'] = 1
    assert second.inputs is first.inputs
    assert WorkflowRunRecord('r3', 'w', 'a', 'm', 'pending', 0, 0, 0).inputs == {}


# LLM: 单次源覆盖只改变子进程输入，不得改写 script.json。
# 函数用途: 验证覆盖与数据源热更新独立于脚本代码。
def test_source_override_is_ephemeral_and_nested_descriptor_supported(tmp_path, monkeypatch):
    folder = script(tmp_path, 'import os,json; print(json.dumps({"value": json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])}))')
    original = json.loads((folder / 'script.json').read_text())
    runtime = {key: original.pop(key) for key in ('entrypoint', 'interpreter', 'timeout_seconds')}
    from agent_py_agent.agent.assets.script_store import ScriptStore
    (folder / 'meta.json').write_text(json.dumps({'descriptor': original, **runtime}))
    monkeypatch.setattr(ScriptStore, 'get_script', lambda *args: None)
    before = (folder / 'meta.json').read_bytes()
    runner = ScriptRunner(tmp_path)
    assert runner.run_script('collect', override_data_sources={'active_appnames': ['C']}).output_data['value'] == {'active_appnames': ['C']}
    assert runner.run_script('collect').output_data['value'][0]['query_scope'] == {'active_appnames': ['A', 'B']}
    assert (folder / 'meta.json').read_bytes() == before


# LLM: 坏配置、目录穿越和静态符号链接不应启动脚本。
# 函数用途: 验证受限解释器、超时和入口路径检查。
@pytest.mark.parametrize('options', [
    {'interpreter': 'python3 -c "pass"'}, {'timeout_seconds': 0},
    {'timeout_seconds': -1}, {'timeout_seconds': float('inf')},
])
def test_script_rejects_bad_configuration(tmp_path, options):
    script(tmp_path, 'print(\'{"value": 1}\')', **options)
    result = ScriptRunner(tmp_path).run_script('collect')
    assert not result.ok and result.error_code == 'SCRIPT_CONFIGURATION_ERROR'
    assert result.exit_code is None


# LLM: 资产外的哨兵文件仅用于观察；验证不得跟随链接读取或执行它。
# 函数用途: 拦截路径穿越、脚本入口链接和账本链接。
def test_path_traversal_and_symlinks_are_rejected(tmp_path):
    folder = script(tmp_path, 'print(\'{"value": 1}\')')
    (folder / 'linked.py').symlink_to(folder / 'main.py')
    metadata = json.loads((folder / 'script.json').read_text())
    metadata['entrypoint'] = 'linked.py'
    (folder / 'script.json').write_text(json.dumps(metadata))
    runner = ScriptRunner(tmp_path)
    assert runner.run_script('collect').error_code == 'SCRIPT_CONFIGURATION_ERROR'
    assert runner.run_script('../collect').error_code == 'SCRIPT_CONFIGURATION_ERROR'
    with pytest.raises(ValueError):
        WorkflowRunStore(tmp_path).load_run('../../outside')
    (tmp_path / 'data').mkdir(exist_ok=True)
    (tmp_path / 'data' / 'workflow_runs').symlink_to(folder)
    with pytest.raises(ValueError):
        WorkflowRunStore(tmp_path)


# LLM: 大量输出不能进入 JSON 解析或作为成功结果，预览长度固定受限。
# 函数用途: 验证输出超限时明确失败并清理进程。
def test_output_limit_and_bounded_previews(tmp_path):
    script(tmp_path, 'print("x" * (2*1024*1024))')
    result = ScriptRunner(tmp_path).run_script('collect')
    assert result.error_code == 'SCRIPT_OUTPUT_LIMIT' and result.needs_repair
    assert len(result.stdout_preview) <= 4096 and len(result.stderr_preview) <= 4096


# LLM: 超时后的同组后代必须停止，不能在父脚本返回后继续写文件。
# 函数用途: 从文件系统观察真实进程树超时清理。
def test_timeout_kills_child_process_group(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner as module
    original = module._execute
    # LLM: 临时执行目录删除之前观察同组子进程的延迟写，避免目录已删除造成假阴性。
    # 函数用途: 在真实执行清理后保留短观察窗口验证后代没有继续写入。
    def observe(command, folder, environment, inputs, timeout, capture):
        result = original(command, folder, environment, inputs, timeout, capture)
        time.sleep(.5)  # 子进程自身延迟为.4秒，观察窗口覆盖该写入时刻。
        assert not (Path(folder) / 'late').exists()
        return result
    monkeypatch.setattr(module, '_execute', observe)
    script(tmp_path, '''import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(.4); Path('late').write_text('leaked')"])
time.sleep(10)
''', timeout_seconds=0.1)
    assert ScriptRunner(tmp_path).run_script('collect').error_code == 'SCRIPT_TIMEOUT'


# LLM: 中断应保存暂停状态和步骤计时，以同一公开入口恢复，不吞用户中断。
# 函数用途: 验证回调被打断后可以续跑且时间账本完整。
def test_interruption_is_persisted_and_resumable(tmp_path):
    # LLM: 同步回调中断必须传播，由引擎只补进度账本。
    # 函数用途: 模拟用户中断分析步骤。
    def interrupt(*args):
        raise KeyboardInterrupt
    engine = WorkflowEngine(tmp_path, analysis_handler=interrupt)
    definition = workflow(step('judge', mode='llm_skill_analysis', script_id=''))
    with pytest.raises(KeyboardInterrupt):
        engine.run_workflow(definition)
    record = engine.store.list_runs()[0]
    assert record.status == 'paused_for_resume'
    assert record.steps[0].status == 'paused_for_resume'
    assert record.steps[0].ended_at >= record.steps[0].started_at > 0
    assert record.ended_at >= record.started_at
    result = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'done': True}).resume_workflow_run(record.workflow_run_id)
    assert result.status == 'succeeded'


# LLM: 恢复结果也必须满足固化脚本的必选输出字段，不能用非法数据推进后步。
# 函数用途: 验证无效恢复数据不被当作自愈成功。
def test_recovered_output_must_satisfy_script_contract(tmp_path):
    script(tmp_path, 'raise RuntimeError("failed")')
    run = WorkflowEngine(tmp_path, escalation_handler=lambda ctx: {'recovered': True, 'output_data': {}}).run_workflow(workflow(step()))
    assert run.status == 'paused_for_resume' and run.failed_step_id == 'collect'
    assert run.steps[0].error_code == 'SCRIPT_MISSING_OUTPUT_KEYS'


# LLM: 错误分析回调和缺失条件不能伪造成功；运行必须停在具体步骤。
# 函数用途: 验证分析异常、无分析器和未定义输出引用。
@pytest.mark.parametrize('kind', ['missing_handler', 'bad_output', 'missing_condition'])
def test_analysis_and_condition_fail_closed(tmp_path, kind):
    handler = (lambda *args: []) if kind == 'bad_output' else None
    condition = {'step_id': 'missing', 'key': 'flag', 'equals': True} if kind == 'missing_condition' else ''
    run = WorkflowEngine(tmp_path, analysis_handler=handler).run_workflow(workflow(step('judge', mode='llm_skill_analysis', script_id='', condition=condition)))
    assert run.status == 'failed' and run.failed_step_id == 'judge'
    assert run.steps[0].llm_involved is (kind == 'bad_output')


# LLM: 无前序成功证据时不得从后续任意步骤起跑，防止漏执行失败步。
# 函数用途: 验证恢复位置和运行执行锁。
def test_resume_cannot_skip_failure_and_run_lock_is_exclusive(tmp_path):
    script(tmp_path, 'raise RuntimeError("failed")')
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow(workflow(step(), step('tail')))
    with pytest.raises(ValueError):
        engine.resume_workflow_run(run.workflow_run_id, 'tail')
    with pytest.raises(ValueError):
        engine.resume_workflow_run(run.workflow_run_id, 'unknown')
    with engine.store.execution_lock(run.workflow_run_id), pytest.raises(ValueError):
        engine.resume_workflow_run(run.workflow_run_id)


# LLM: 单步快照已发表但 run.json 尚未更新的崩溃窗口，仍要读取成功快照。
# 函数用途: 验证独立步骤快照的恢复优先级与原子写失败清理。
def test_store_uses_step_snapshot_and_atomic_failure_preserves_old(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows.run_store as module
    store = WorkflowRunStore(tmp_path)
    record = WorkflowRunRecord('crash', 'w', 'a', 'manual', 'running', 0, 0, 0,
                               steps=(StepExecutionRecord('one', 'one', 'deterministic_script', 'running', 0, 0, 0, False),))
    store.create_run(record)
    folder = tmp_path / 'data' / 'workflow_runs' / 'crash'
    snapshot = asdict(replace(record.steps[0], status='succeeded', output_snapshot={'value': 1}))
    (folder / 'steps' / 'one.json').write_text(json.dumps(snapshot))
    assert store.load_run('crash').steps[0].output_snapshot == {'value': 1}
    before = (folder / 'run.json').read_bytes()
    # LLM: 仅打断本次原子替换，不修改既存字节。
    # 函数用途: 模拟磁盘写入失败。
    def fail_replace(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(module, 'write_text_atomic_beneath', fail_replace)
    with pytest.raises(OSError):
        store.save_run(replace(record, status='failed'))
    assert (folder / 'run.json').read_bytes() == before
    assert not list(folder.rglob('workflow-*.tmp'))


# LLM: 模拟在后缀首步 running 发表后进程被打断，不能仅在内存保留跳过标记。
# 函数用途: 验证跨进程恢复不会将前步跳过决定错误扩散到无关尾步。
def test_resume_restores_false_condition_skip_tail(tmp_path, monkeypatch):
    script(tmp_path, 'print(\'{"value": 0, "flag": false}\')')
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(args) or {'unexpected': True})
    original = engine.store.save_step_result
    # LLM: 先执行真实持久化，再在目标窗口中断以模拟进程死亡。
    # 函数用途: 在尾步 running 已发表时停止本次运行。
    def interrupt_after_running(run_id, record):
        saved = original(run_id, record)
        if record.step_id == 'tail' and record.status == 'running':
            raise KeyboardInterrupt
        return saved
    condition = {'step_id': 'collect', 'key': 'flag', 'equals': True}
    with monkeypatch.context() as patch:
        patch.setattr(engine.store, 'save_step_result', interrupt_after_running)
        with pytest.raises(KeyboardInterrupt):
            engine.run_workflow(workflow(step(), step('judge', condition=condition),
                                         step('tail', mode='llm_skill_analysis', script_id='')))
    previous = engine.store.list_runs()[0]
    assert previous.steps[1].status == 'skipped'
    resumed = engine.resume_workflow_run(previous.workflow_run_id)
    assert resumed.status == 'succeeded' and resumed.steps[-1].status == 'succeeded'
    assert len(calls) == 1 and resumed.steps[-1].llm_involved


# LLM: 极深 JSON 是输出格式错误，不得让 RecursionError 穿透到调用方绕过修复链。
# 函数用途: 验证深层输出仍能返回规范失败结果并唤醒允许的兜底。
def test_deep_json_output_remains_structured_and_escalates(tmp_path):
    script(tmp_path, 'print(\'{"value":\' + "[" * 2000 + "0" + "]" * 2000 + "}")')
    result = ScriptRunner(tmp_path).run_script('collect')
    assert result.error_code == 'SCRIPT_INVALID_JSON' and result.needs_repair
    calls = []
    # LLM: 仅记录升级请求，返回符合输出合同的应急数据。
    # 函数用途: 证明深层 JSON 错误仍进入已允许的自愈回调。
    def handler(ctx):
        calls.append(ctx)
        return {'recovered': True, 'output_data': {'value': 2}}
    run = WorkflowEngine(tmp_path, escalation_handler=handler).run_workflow(workflow(step()))
    assert run.status == 'succeeded' and len(calls) == 1


# LLM: 逻辑编号校验必须覆盖实际快照文件名，失败不能留下会污染 list_runs 的残缺账本。
# 函数用途: 验证过长步骤在创建运行前被拒绝。
def test_long_step_id_rejected_before_creating_run(tmp_path):
    engine = WorkflowEngine(tmp_path)
    with pytest.raises(ValueError):
        engine.run_workflow(workflow(step('s' * 124)))
    assert engine.store.list_runs() == []


# LLM: 裸字符串不是技能编号容器，禁止拆成字符后触发模型回调。
# 函数用途: 对静态编号、标签和动态结果逐项校验容器类型。
@pytest.mark.parametrize('kind', ['ids', 'tags', 'resolver'])
def test_skill_sequences_reject_bare_strings(tmp_path, kind):
    calls = []
    spec = step('judge', mode='llm_skill_analysis', script_id='',
                skill_ids='sales' if kind == 'ids' else ['static'],
                skill_tags='sales' if kind == 'tags' else ['tag'])
    resolver = (lambda *args: 'sales') if kind == 'resolver' else None
    run = WorkflowEngine(tmp_path, skill_resolver=resolver,
                         analysis_handler=lambda *args: calls.append(args) or {}).run_workflow(workflow(spec))
    assert run.status == 'failed' and run.failed_step_id == 'judge'
    assert calls == [] and run.steps[0].llm_involved is False


# LLM: 同时覆盖磁盘资产入口与可序列化 dataclass 协议，避免只测手工字典。
# 函数用途: 验证dataclass定义以及Store明确缺失时的安全回退；正式Store入口另有真实集成测试。
def test_dataclass_and_missing_store_fallback_entrypoints(tmp_path, monkeypatch):
    # LLM: 测试只复现资产定义的序列化合同，不依赖并行模块完成时序。
    # 类用途: 提供嵌套描述及元组步骤的最小数据结构。
    @dataclass
    class Definition:
        descriptor: dict
        steps: tuple

    script(tmp_path, 'print(\'{"value": 1}\')')
    definition = Definition({'id': 'daily', 'owner_id': 'alice'}, (step(),))
    folder = tmp_path / 'workflows' / 'daily'
    folder.mkdir(parents=True)
    (folder / 'workflow.json').write_text(json.dumps(asdict(definition)))
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    monkeypatch.setattr(WorkflowStore, 'get_workflow', lambda *args: None)
    engine = WorkflowEngine(tmp_path)
    assert engine.run_workflow(definition, owner_id='alice').final_output == {'value': 1}
    assert engine.run_workflow('daily', owner_id='alice').status == 'succeeded'
    with pytest.raises(ValueError):
        engine.run_workflow('daily', owner_id='bob')


# LLM: 预览截断不能导致完整 JSON 被错误截断后再解析；字符串里的括号不增加结构深度。
# 函数用途: 验证较大的合法输出仍完整解析，词法深度检查不会误伤普通文本。
def test_full_json_parsed_before_preview_truncation(tmp_path):
    script(tmp_path, '''import json
print(json.dumps({'value': 'x' * 6000, 'text': '[' * 200 + '"'}))
''')
    result = ScriptRunner(tmp_path).run_script('collect')
    assert result.ok and result.output_data['value'] == 'x' * 6000
    assert result.output_data['text'] == '[' * 200 + '"'
    assert len(result.stdout_preview) == 4096


# LLM: 直接使用同批资产模块写出的权威格式，只在 pytest 临时 Owner 内执行无害脚本。
# 函数用途: 验证真实资产存储到工作流执行的接口，以及源配置热更新无需改代码。
def test_real_asset_store_to_engine_and_hot_sources(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor, DataSourceSpec, IOContract
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import (
        WorkflowDefinition,
        WorkflowStepSpec,
        WorkflowStore,
    )

    scripts = ScriptStore(tmp_path)
    sources = (DataSourceSpec('logs', 'logging', {'active_appnames': ['A', 'B']}),)
    descriptor = AssetDescriptor('collect', 'script', '采集', owner_id='alice',
                                 data_sources=sources, io_contract=IOContract(outputs=('value',)))
    code = '''import json,os
sources=json.loads(os.environ['MY_AGENT_DATA_SOURCES_JSON'])
print(json.dumps({'value':len(sources[0]['query_scope']['active_appnames'])}))
'''
    scripts.save_script(descriptor, code)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报', owner_id='alice'),
                                    (WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect'),))
    WorkflowStore(tmp_path).save_workflow(definition)
    engine = WorkflowEngine(tmp_path)
    assert engine.run_workflow('daily', owner_id='alice').final_output == {'value': 2}
    scripts.update_data_sources('collect', 'logs', query_scope={'active_appnames': ['A', 'B', 'C']})
    assert engine.run_workflow(definition, owner_id='alice').final_output == {'value': 3}
    assert scripts.get_script('collect')['code'] == code


# LLM: 模拟真实脚本退出后的清理拒绝，不能让次级异常覆盖原失败或被吞成成功。
# 函数用途: 验证输出超限和无业务错误两种场景都完整保留清理失败上下文。
@pytest.mark.parametrize(('code', 'expected_error', 'expected_exit'), [
    ('print("x" * (2*1024*1024))', 'SCRIPT_OUTPUT_LIMIT', 0),
    ('print(\'{"value": 1}\')', 'SCRIPT_CLEANUP_ERROR', 0),
    ('raise SystemExit(4)', 'SCRIPT_EXIT_NONZERO', 4),
    ('print("invalid json")', 'SCRIPT_INVALID_JSON', 0),
])
@pytest.mark.parametrize('recovery', [None, {'recovered': True, 'output_data': {'value': 9}}])
def test_cleanup_denial_preserves_primary_failure_and_context(tmp_path, monkeypatch, code, expected_error, expected_exit, recovery):
    import errno

    from agent_py_agent.agent.workflows import script_runner as module

    # LLM: 先回收真实子进程，随后只在清理边界注入权限拒绝，不留活进程或伪造业务输出。
    # 函数用途: 确定性重放清理时 PermissionError 覆盖输出结果的窗口。
    def deny_cleanup(process):
        process.wait(timeout=3)
        raise PermissionError(errno.EPERM, 'simulated cleanup denial')

    script(tmp_path, code)
    monkeypatch.setattr(module, '_terminate', deny_cleanup)
    result = ScriptRunner(tmp_path).run_script('collect')
    assert result.error_code == expected_error, result.escalation_context
    assert result.ok is False and result.needs_repair is True
    assert result.exit_code == expected_exit
    cleanup = result.escalation_context['cleanup_error']
    assert cleanup['error_code'] == 'SCRIPT_CLEANUP_ERROR'
    assert cleanup['exception_type'] == 'PermissionError' and cleanup['errno'] == errno.EPERM
    assert cleanup['leader_exited'] is True and cleanup['group_cleanup_verified'] is False
    contexts = []
    run = WorkflowEngine(tmp_path, escalation_handler=lambda ctx: contexts.append(ctx) or recovery).run_workflow(workflow(step()))
    assert run.status == 'paused_for_resume' and len(contexts) == 1
    assert contexts[0]['cleanup_error']['errno'] == errno.EPERM
    assert run.steps[0].error_code == expected_error
    assert run.steps[0].escalation_result['cleanup_error']['group_cleanup_verified'] is False


# LLM: 清理后输出读取再失败时，已确认的清理硬失败不能在元组返回前丢失并被兜底绕过。
# 函数用途: 重放权限拒绝后 fstat 报错，验证回调仍不能把步骤改成恢复成功。
def test_post_cleanup_io_failure_keeps_cleanup_hard_failure(tmp_path, monkeypatch):
    import errno

    from agent_py_agent.agent.workflows import script_runner as module

    state, contexts = {'after_cleanup': False}, []
    original_fstat = module.os.fstat
    # LLM: 只在真实子进程退出后注入拒绝，避免测试创建无法回收的活进程。
    # 函数用途: 建立清理失败已发生的确定性边界。
    def deny_cleanup(process):
        process.wait(timeout=3)
        state['after_cleanup'] = True
        raise PermissionError(errno.EPERM, 'simulated cleanup denial')

    # LLM: 前置真实 I/O 不变；清理完成后才注入输出采样失败。
    # 函数用途: 暴露执行函数异常退出时清理事实丢失的路径。
    def fail_post_cleanup_stat(fd):
        if state['after_cleanup']:
            state['after_cleanup'] = False
            raise OSError(errno.EIO, 'simulated output read failure')
        return original_fstat(fd)

    script(tmp_path)
    monkeypatch.setattr(module, '_terminate', deny_cleanup)
    monkeypatch.setattr(module.os, 'fstat', fail_post_cleanup_stat)
    run = WorkflowEngine(tmp_path, escalation_handler=lambda ctx: contexts.append(ctx) or {'recovered': True, 'output_data': {'value': 9}}).run_workflow(workflow(step()))
    assert run.status == 'paused_for_resume'
    assert contexts[0]['cleanup_error']['errno'] == errno.EPERM
    assert contexts[0]['execution_error']['errno'] == errno.EIO
    assert run.steps[0].escalation_result['cleanup_error']['group_cleanup_verified'] is False


# LLM: 策略错误必须在创建运行前拒绝，不能悄悄落入失败或升级路径。
# 函数用途: 守住公开定义与字典运行入口的错误策略枚举。
def test_on_error_policy_rejected_before_run(tmp_path):
    from agent_py_agent.agent.workflows import models
    assert hasattr(models, 'WORKFLOW_ON_ERROR_POLICIES')
    assert models.WORKFLOW_ON_ERROR_POLICIES == ('escalate_to_agent', 'fail', 'skip')
    assert hasattr(models, 'WorkflowStepSpec')
    with pytest.raises(ValueError, match='Unsupported on_error policy: typo'):
        models.WorkflowStepSpec('s', 's', 'deterministic_script', on_error='typo')
    engine = WorkflowEngine(tmp_path)
    with pytest.raises(ValueError, match='Unsupported on_error policy: typo'):
        engine.run_workflow(workflow(step(on_error='typo')))
    assert engine.store.list_runs() == []


# LLM: 跳过决定仅属于当前步骤，后续无条件脚本不能被隐式锁死。
# 函数用途: 验证假条件之后的独立步骤仍产生真实输出。
def test_false_condition_does_not_skip_unrelated_step(tmp_path):
    script(tmp_path)
    run = WorkflowEngine(tmp_path).run_workflow(workflow(
        step(), step('conditional', condition={'step_id': 'collect', 'key': 'value', 'equals': 0}), step('tail')))
    assert [item.status for item in run.steps] == ['succeeded', 'skipped', 'succeeded']
    assert run.status == 'succeeded' and run.steps[2].output_snapshot == {'value': 1}


# LLM: skip策略保留诊断但不阻断后续独立步骤，也不把跳过输出传给后步。
# 函数用途: 验证失败脚本按显式策略跳过并完成工作流。
def test_on_error_skip_continues_after_failure(tmp_path):
    script(tmp_path, 'raise SystemExit(4)', script_id='bad')
    script(tmp_path)
    run = WorkflowEngine(tmp_path).run_workflow(workflow(step('bad', script_id='bad', on_error='skip'), step()))
    assert run.status == 'succeeded' and not run.needs_repair
    assert [item.status for item in run.steps] == ['skipped', 'succeeded']
    assert run.steps[0].error_code == 'SCRIPT_EXIT_NONZERO' and not run.steps[0].needs_repair


# LLM: 默认升级无处理器时必须保留可恢复诊断，重启后从该步续跑并清除修复标记。
# 函数用途: 验证暂停、磁盘往返、脚本修复和公开续跑入口。
def test_default_escalation_pauses_and_resumes(tmp_path):
    folder = script(tmp_path, 'raise SystemExit(4)')
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow(workflow(step(), step('tail')))
    assert run.status == 'paused_for_resume' and run.needs_repair
    assert run.failed_step_id == 'collect' and run.steps[0].needs_repair
    assert run.steps[0].status == 'paused_for_resume' and run.steps[1].status == 'pending'
    assert run.steps[0].escalation_result['error_code'] == 'SCRIPT_EXIT_NONZERO'
    assert engine.store.load_run(run.workflow_run_id) == run
    assert WorkflowRunRecord.from_dict(run.to_dict()) == run
    assert StepExecutionRecord.from_dict(run.steps[0].to_dict()) == run.steps[0]
    (folder / 'main.py').write_text('print(\'{"value": 3}\')')
    resumed = WorkflowEngine(tmp_path).resume_workflow_run(run.workflow_run_id)
    assert resumed.status == 'succeeded' and resumed.failed_step_id == ''
    assert not resumed.needs_repair and all(not item.needs_repair for item in resumed.steps)


# LLM: fail策略与升级暂停是不同状态，失败记录须显式携带修复需求。
# 函数用途: 验证不可恢复策略的运行和步骤标记完整落盘。
def test_on_error_fail_sets_repair_flags(tmp_path):
    script(tmp_path, 'raise SystemExit(4)')
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow(workflow(step(on_error='fail'), step('tail')))
    assert run.status == 'failed' and run.needs_repair
    assert run.steps[0].status == 'failed' and run.steps[0].needs_repair
    assert run.failed_step_id == 'collect' and run.steps[1].status == 'pending'
    assert engine.store.load_run(run.workflow_run_id) == run


# LLM: 新处理器接收步骤、脚本结果和运行记录；只接受合同内修复输出，不执行修复草案。
# 函数用途: 验证三参升级协议、上下文脱敏和恢复结果写入。
def test_structured_escalation_handler_protocol(tmp_path):
    script(tmp_path, 'raise SystemExit(4)')
    seen = []
    # LLM: 回调只消费结构化数据，不写脚本或运行账本。
    # 函数用途: 返回合同合法的应急输出并观察调用协议。
    def recover(spec, result, run):
        seen.append((spec.step_id, result.error_code, run.workflow_id))
        return {'recovered': True, 'patched_output': {'value': 8}, 'password': 'must-redact'}
    run = WorkflowEngine(tmp_path, escalation_handler=recover).run_workflow(workflow(step()))
    assert seen == [('collect', 'SCRIPT_EXIT_NONZERO', 'daily')]
    assert run.status == 'succeeded' and run.steps[0].status == 'succeeded'
    assert run.final_output == {'value': 8} and not run.needs_repair
    assert run.steps[0].escalation_result['password'] != 'must-redact'


# LLM: 严格Store校验报错不能当作不存在而降级；嵌套旧磁盘格式不再绕过正式资产校验。
# 函数用途: 验证真实Store拒绝非法存量定义且未触发回退或创建运行。
def test_invalid_store_definition_never_falls_back(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import engine as module
    folder = tmp_path / 'workflows' / 'daily'
    folder.mkdir(parents=True)
    (folder / 'workflow.json').write_text(json.dumps(workflow(step())))
    # LLM: 回退若被调用立即失败；不伪造Store返回值。
    # 函数用途: 观察损坏资产是否错误进入兼容读取链。
    def forbidden(*args):
        pytest.fail('Store校验失败不应触发fallback')
    monkeypatch.setattr(module, 'read_text_beneath', forbidden)
    engine = WorkflowEngine(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        engine.run_workflow('daily')
    assert engine.store.list_runs() == []
