"""R4-P1-02：同一注册 WorkflowsTool 上重叠的 run/resume 各自持有本次冻结的 runner。

A 停在分析屏障时，B 以不同 RunParams 和仅 B 可写的输出目录启动并完成；A 放行后的脚本步骤必须仍用 A 自己
冻结的 run/task 身份与路径范围。Agent、self-map 注册、runner_options_provider、缓存 engine、WorkflowRunStore
与真实 Python 守卫子进程都是生产实现；只把会调用模型的分析回调换成 Event 屏障，等待全部有上界、不靠 sleep。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import workflows_tool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

# 屏障等待上界（秒），只作失败兜底；正常路径由 Event 立即唤醒。
_WAIT = 30
_RECEIPT = ('workflow_run_id', 'workflow_id', 'status', 'phase', 'effect_outcome',
            'execution_definition_ref', 'confirmed_step_refs')

# 只报告 runner 注入的冻结身份，并向自己/对方的文件各追加一行；拒写只认守卫的 PermissionError。
_SCOPE_PROBE = '''import json, os, sys
inputs = json.load(sys.stdin)
context = json.loads(os.environ['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'])
result = {'run_id': context['run_id'], 'task_id': context['task_id'],
          'write_roots': context['effective_path_scope']['write_roots']}
for name in ('own', 'foreign'):
    if name in inputs:
        try:
            with open(inputs[name], 'a', encoding='utf-8') as handle:
                handle.write(inputs['text'] + '\\n')
            result[name] = True
        except PermissionError:
            result[name] = False
print(json.dumps(result))
'''


class _AnalysisGate:
    """分析回调替身：pause_once 中的步骤首调返回 _pause_for_resume，其余调用停在屏障直到放行。"""

    def __init__(self):
        self.reached, self.release = threading.Event(), threading.Event()
        self.pause_once, self.calls = set(), []

    def __call__(self, spec, context, resolved):
        self.calls.append(spec['step_id'])
        if spec['step_id'] in self.pause_once:
            self.pause_once.discard(spec['step_id'])
            return {'_pause_for_resume': True}
        self.reached.set()
        if not self.release.wait(_WAIT):
            raise TimeoutError('analysis barrier was never released')
        return {'analyzed': spec['step_id']}


@pytest.fixture
def case(tmp_path, monkeypatch):
    # venv 里有旧检出的 editable 安装；被测模块必须来自当前工作树，不能把旧代码的绿灯算进来。
    expected = Path(__file__).resolve().parents[1]
    assert Path(workflows_tool.__file__).resolve().is_relative_to(expected)
    assert Path(script_runner.__file__).resolve().is_relative_to(expected)
    (tmp_path / 'project').mkdir()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), memory_path='memory.jsonl',
                                    orphan_supervision_interval_seconds=0), tmp_path / 'project')
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    workspace = owner / 'tasks/2026-11-01/business'
    workspace.mkdir(parents=True)
    agent.effective_workspace_root = workspace
    # 只选择已有的非 strict 外层 Seatbelt 回退；写入仍由真实 Python 守卫按冻结范围裁决。
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    tool = agent.tools.tools['workflows']
    assert getattr(tool, '_lazy_engine', None) is None
    gate = _AnalysisGate()
    tool.analysis_handler = gate
    frozen, provider = [], tool.runner_options_provider

    # 仍调用注册的生产 provider，只记录每次被读取时冻结的 run/task。
    def observed_provider():
        options = provider()
        frozen.append((options['work_context']['run_id'], options['work_context']['task_id']))
        return options

    tool.runner_options_provider = observed_provider
    ScriptStore(owner, workspace_root=workspace).save_script(
        AssetDescriptor('scope-probe', 'script', 'scope-probe'), _SCOPE_PROBE)
    out_a, out_b = tmp_path / 'out-a', tmp_path / 'out-b'
    out_a.mkdir()
    out_b.mkdir()
    return SimpleNamespace(agent=agent, tool=tool, owner=owner, workspace=workspace, gate=gate,
                           frozen=frozen, out_a=out_a.resolve(), out_b=out_b.resolve())


# 每个宿主运行只授权自己的墙外输出目录；owner home 是两者共有的产品工作区。
def _params(run_id, task_id, output_dir):
    return RunParams(run_id=run_id, task_id=task_id, attempt_id=f'{run_id}-attempt', task_attributes={},
                     write_boundary={'allowed_write_roots': [str(output_dir)]})


def _probe_step(step_id, own, foreign=None, text=''):
    inputs = {'own': str(own), 'text': text, **({'foreign': str(foreign)} if foreign is not None else {})}
    return {'step_id': step_id, 'name': step_id, 'mode': 'deterministic_script', 'script_id': 'scope-probe',
            'inputs_map': inputs, 'on_error': 'fail'}


def _analysis_step(step_id):
    return {'step_id': step_id, 'name': step_id, 'mode': 'llm_skill_analysis', 'on_error': 'fail'}


def _save_flow(case, workflow_id, *steps):
    definition = {'descriptor': AssetDescriptor(workflow_id, 'workflow', workflow_id).to_dict(),
                  'steps': list(steps)}
    saved = case.tool.execute({'action': 'save', 'workflow_id': workflow_id, 'definition': definition})
    assert saved.ok, saved.output


# B 只写自己的目录，并尝试写 A 的目录以证明 B 的范围同样只属于 B。
def _save_flow_b(case):
    _save_flow(case, 'flow-b', _probe_step('write', case.out_b / 'report.txt', case.out_a / 'b.txt', 'B'))


def _execute(tool, request):
    outcome = tool.execute(request)
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)


# A 在自己的运行线程进入 run/resume 并停在屏障；期间主线程以 B 的 RunParams 同步跑完 B，再放行 A。
def _overlap(case, params_a, request_a, params_b, request_b):
    box = {}

    def invoke():
        try:
            # _current_run_params 是按线程绑定的宿主运行事实，与生产中每个 run 各占一个工作线程一致。
            case.agent._current_run_params = params_a
            box['outcome'] = case.tool.execute(request_a)
        except BaseException as exc:  # 交回主线程断言，不让线程异常被吞
            box['error'] = exc
        finally:
            case.gate.reached.set()  # A 若没到屏障就结束，也立即唤醒主线程

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    try:
        assert case.gate.reached.wait(_WAIT), 'A 未到达分析屏障'
        assert not box, box  # A 仍停在本次调用内部
        case.agent._current_run_params = params_b
        result_b = _execute(case.tool, request_b)
    finally:
        case.gate.release.set()
        thread.join(_WAIT)
    assert not thread.is_alive() and 'error' not in box, box
    assert box['outcome'].ok, box['outcome'].output
    return json.loads(box['outcome'].output), result_b


# 经只读 inspect_run 读原持久账本：两条运行回执互不覆盖，且与各自调用当时返回的一致。
def _assert_distinct_receipts(case, *runs):
    records = []
    for run, workflow_id, step_refs in runs:
        record = _execute(case.tool, {'action': 'inspect_run', 'workflow_run_id': run['workflow_run_id']})
        assert {key: record[key] for key in _RECEIPT} == {key: run[key] for key in _RECEIPT}
        assert record['steps'] == run['steps']
        assert (record['workflow_id'], record['status'], record['phase'], record['effect_outcome']) == (
            workflow_id, 'succeeded', 'terminal', 'confirmed')
        assert record['confirmed_step_refs'] == step_refs
        records.append(record)
    first, second = records
    assert first['workflow_run_id'] != second['workflow_run_id']
    assert first['execution_definition_ref'] != second['execution_definition_ref']
    assert len(WorkflowRunStore(case.owner).list_runs()) == 2


def _tree_bytes(folder):
    return {path.relative_to(folder): path.read_bytes() for path in sorted(folder.rglob('*')) if path.is_file()}


def test_waiting_run_keeps_its_own_runner_when_another_run_starts(case, monkeypatch):
    out_a, out_b = case.out_a, case.out_b
    _save_flow(case, 'flow-a', _analysis_step('analyze'),
               _probe_step('probe', out_a / 'a.txt', out_b / 'report.txt', 'A'))
    _save_flow_b(case)
    engines = []

    # 只观察真实 _execution_engine 交给每次调用的执行器，不替换其行为。
    def observed_engine():
        engine = WorkflowsTool._execution_engine(case.tool)
        engines.append(engine)
        return engine

    monkeypatch.setattr(case.tool, '_execution_engine', observed_engine)
    run_a, run_b = _overlap(case, _params('run-a', 'task-a', out_a), {'action': 'run', 'workflow_id': 'flow-a'},
                            _params('run-b', 'task-b', out_b), {'action': 'run', 'workflow_id': 'flow-b'})
    probe, written = run_a['steps'][1]['output_snapshot'], run_b['steps'][0]['output_snapshot']
    # A 放行后的脚本仍用 A 冻结的身份与范围：写不进 B 的目录，B 的产物原样保留。
    assert (probe['run_id'], probe['task_id']) == ('run-a', 'task-a')
    assert (probe['own'], probe['foreign']) == (True, False)
    assert str(out_a) in probe['write_roots'] and str(out_b) not in probe['write_roots']
    assert (written['run_id'], written['task_id'], written['own'], written['foreign']) == (
        'run-b', 'task-b', True, False)
    assert (out_b / 'report.txt').read_text() == 'B\n'
    assert (out_a / 'a.txt').read_text() == 'A\n' and not (out_a / 'b.txt').exists()
    # 每次调用各得一个浅拷贝：只有 runner 不同，RunStore/回调/解析器/workspace 都与缓存共用。
    cached = case.tool._lazy_engine
    engine_a, engine_b = engines
    assert engine_a is not engine_b and cached is not engine_a and cached is not engine_b
    for engine in engines:
        shared = {key: value for key, value in vars(engine).items() if key != 'runner'}
        assert shared.keys() == vars(cached).keys() - {'runner'}
        assert all(value is vars(cached)[key] for key, value in shared.items())
    assert (engine_a.runner.work_context['run_id'], engine_b.runner.work_context['run_id']) == ('run-a', 'run-b')
    assert cached.runner.work_context is None  # 缓存从不持有任一调用的 runner
    _assert_distinct_receipts(case, (run_a, 'flow-a', ['analyze', 'probe']), (run_b, 'flow-b', ['write']))
    # provider 每次调用只冻结一次，A 的后续步骤和只读 inspect_run 都没有再读活动上下文。
    assert case.frozen == [('run-a', 'task-a'), ('run-b', 'task-b')] and len(engines) == 2


def test_resume_keeps_its_own_runner_and_successful_prefix_when_another_run_starts(case):
    out_a, out_b = case.out_a, case.out_b
    _save_flow(case, 'flow-r', _probe_step('prefix', out_a / 'prefix.txt', text='prefix'), _analysis_step('gate'),
               _probe_step('probe', out_a / 'a.txt', out_b / 'report.txt', 'A'))
    _save_flow_b(case)
    case.gate.pause_once.add('gate')
    case.agent._current_run_params = _params('run-a', 'task-a', out_a)
    paused = _execute(case.tool, {'action': 'run', 'workflow_id': 'flow-r'})
    assert (paused['status'], paused['confirmed_step_refs']) == ('paused_for_resume', ['prefix'])
    resumed, run_b = _overlap(
        case, _params('run-a-resume', 'task-a', out_a), {'action': 'resume', 'workflow_run_id': paused['workflow_run_id']},
        _params('run-b', 'task-b', out_b), {'action': 'run', 'workflow_id': 'flow-b'})
    prefix, probe = resumed['steps'][0], resumed['steps'][2]['output_snapshot']
    # 成功前缀不重跑：步骤记录与暂停时完全一致，其唯一一次副作用也只出现一次。
    assert prefix == paused['steps'][0] and prefix['output_snapshot']['run_id'] == 'run-a'
    assert (out_a / 'prefix.txt').read_text() == 'prefix\n'
    assert case.gate.calls == ['gate', 'gate']
    # 续跑脚本用续跑调用自己冻结的身份与范围，而不是后启动的 B。
    assert (probe['run_id'], probe['task_id'], probe['own'], probe['foreign']) == (
        'run-a-resume', 'task-a', True, False)
    assert (out_b / 'report.txt').read_text() == 'B\n' and (out_a / 'a.txt').read_text() == 'A\n'
    assert resumed['workflow_run_id'] == paused['workflow_run_id']
    assert case.tool._lazy_engine.runner.work_context is None
    _assert_distinct_receipts(case, (resumed, 'flow-r', ['prefix', 'gate', 'probe']), (run_b, 'flow-b', ['write']))
    assert case.frozen == [('run-a', 'task-a'), ('run-a-resume', 'task-a'), ('run-b', 'task-b')]


def test_injected_engine_serves_run_and_resume_unchanged(case):
    _save_flow(case, 'flow-r', _probe_step('prefix', case.out_a / 'prefix.txt', text='prefix'), _analysis_step('gate'))
    gate = _AnalysisGate()
    gate.pause_once.add('gate')
    gate.release.set()
    injected = WorkflowEngine(case.owner, workspace_root=case.workspace, analysis_handler=gate)
    runner = injected.runner
    tool = WorkflowsTool(case.owner, engine=injected, workspace_root=case.workspace,
                         runner_options_provider=case.tool.runner_options_provider)
    case.agent._current_run_params = _params('run-a', 'task-a', case.out_a)
    assert tool._execution_engine() is injected
    paused = _execute(tool, {'action': 'run', 'workflow_id': 'flow-r'})
    resumed = _execute(tool, {'action': 'resume', 'workflow_run_id': paused['workflow_run_id']})
    assert (paused['status'], resumed['status']) == ('paused_for_resume', 'succeeded')
    assert resumed['steps'][0] == paused['steps'][0] and (case.out_a / 'prefix.txt').read_text() == 'prefix\n'
    assert _execute(tool, {'action': 'inspect_run', 'workflow_run_id': paused['workflow_run_id']}) == resumed
    # 显式注入的执行器原样承担 run/resume：不复制、不换 runner、不读宿主 provider，也不建缓存 engine。
    assert injected.runner is runner and case.frozen == [] and gate.calls == ['gate', 'gate']
    assert not hasattr(tool, '_lazy_engine')


def test_inspect_run_reads_durable_record_without_building_execution_context(case):
    _save_flow(case, 'flow-r', _probe_step('prefix', case.out_a / 'prefix.txt', text='prefix'), _analysis_step('gate'))
    case.gate.pause_once.add('gate')
    case.agent._current_run_params = _params('run-a', 'task-a', case.out_a)
    paused = _execute(case.tool, {'action': 'run', 'workflow_id': 'flow-r'})
    cached = case.tool._lazy_engine
    runner, folder = cached.runner, cached.store.run_dir(paused['workflow_run_id'])
    before = _tree_bytes(folder)
    # 换成另一个活动上下文：只读检查既不能冻结它，也不能改写原持久账本。
    case.agent._current_run_params = _params('run-b', 'task-b', case.out_b)
    inspected = _execute(case.tool, {'action': 'inspect_run', 'workflow_run_id': paused['workflow_run_id']})
    assert inspected == paused
    assert case.frozen == [('run-a', 'task-a')] and case.gate.calls == ['gate']
    assert case.tool._lazy_engine is cached and cached.runner is runner and runner.work_context is None
    assert _tree_bytes(folder) == before


# LLM: 只观察原解析器实参，宿主范围变化不能回写旧执行器，空受限快照不可变成无限制。
# 函数用途: 验证并发执行器的Skill范围和runner身份分别冻结。
def test_run_skill_snapshots_remain_frozen_when_host_snapshot_changes(case, monkeypatch):
    active = [None]
    case.tool.skill_snapshot_provider = lambda: active[0]
    observed = []
    original = workflows_tool._resolve_owner_skills

    def resolve(home, step, context, **options):
        observed.append(options['skill_snapshot'])
        return original(home, step, context, **options)

    monkeypatch.setattr(workflows_tool, '_resolve_owner_skills', resolve)
    case.agent._current_run_params = _params('snapshot-a', 'task-a', case.out_a)
    first = case.tool._execution_engine()
    cached = case.tool._lazy_engine
    active[0] = {}  # 空受限快照与无快照不同，不能复用原无限制解析器。
    case.agent._current_run_params = _params('snapshot-b', 'task-b', case.out_b)
    second = case.tool._execution_engine()
    case.tool.skill_snapshot_provider = lambda: pytest.fail('execution reread the current host snapshot')
    spec = SimpleNamespace(skill_ids=(), skill_tags=())
    assert first.skill_resolver(spec, {}) == second.skill_resolver(spec, {}) == []
    assert observed == [None, {}]
    assert first.skill_resolver is cached.skill_resolver
    assert second.skill_resolver is not cached.skill_resolver
    assert first.store is second.store is cached.store
    assert first.runner is not second.runner and cached.runner.work_context is None
    assert case.frozen == [('snapshot-a', 'task-a'), ('snapshot-b', 'task-b')]


# LLM: 两条引擎装配路径的全部回调均检查，不允许执行时重读后来宿主范围。
# 函数用途: 静态保护已冻结的解析器，防止缓存回调重新变成动态取权。
def test_cached_skill_resolvers_never_read_live_scope_inside_callback():
    import ast
    import inspect
    import textwrap

    for method in (WorkflowsTool._engine, WorkflowsTool._execution_engine):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        callbacks = [node for node in ast.walk(tree) if isinstance(node, ast.Lambda)]
        assert callbacks
        for callback in callbacks:
            assert not any(isinstance(node, ast.Attribute) and node.attr == '_skill_options'
                           for node in ast.walk(callback))
