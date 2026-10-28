# LLM: A2单节点调试合同回归；脚本诊断走stderr及真实入口透传spy，不写沙箱只读Owner或替换沙箱判断。
# 模块用途: 锁定安全默认、新旧DTO、节点别名、审批/通知及零正式运行账本。
from __future__ import annotations

import json
from dataclasses import fields

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_wave9_host_substrate_plugins_workflow_auth import definition, script


# LLM: 保存完整资产后读取真实磁盘快照；不创建正式工作流run。
# 函数用途: 为debug场景建立一个配置化的目标节点。
def save_node(home, mode, *, config=None, **extra):
    spec = {'step_id': 'target', 'mode': mode, **extra}
    if config is not None:
        spec['config'] = config
    return WorkflowStore(home).save_workflow(definition(spec))


# LLM: 保存原类方法再透传；不替换执行结果、沙箱或进程，供Python及HTTP夹具共用观察点。
# 函数用途: 记录真实脚本入口被调用的次数，配合stderr/输出证明是否执行。
def observe_script_runs(monkeypatch):
    calls = []
    original = ScriptRunner.run_script

    # LLM: 只记录入口调用后透传原run_script，不替换沙箱、子进程、参数或真实返回值。
    # 函数用途: 让测试数清脚本实际调用次数，同时保留原执行链。
    def execute(runner, script_id, *args, **kwargs):
        calls.append(script_id)
        return original(runner, script_id, *args, **kwargs)

    monkeypatch.setattr(ScriptRunner, 'run_script', execute)
    return calls


# LLM: 两次调用同一真实脚本；stderr可穿过真实OS沙箱，Owner只读保护和原输出断言不变。
# 函数用途: 用真实执行计数、子进程诊断及输出三重检查安全默认与显式执行。
@pytest.mark.parametrize('mode', ['deterministic_script', 'script'])
def test_debug_default_does_not_spawn_marker_script_but_explicit_true_does(tmp_path, monkeypatch, mode):
    calls = observe_script_runs(monkeypatch)
    script(tmp_path, 'marker', 'import sys; print("MARKER_EXECUTED", file=sys.stderr); print(\'{"value":8}\')')
    save_node(tmp_path, mode, script_id='marker')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')}
    engine = WorkflowEngine(tmp_path)
    preview = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 3})
    assert calls == [] and 'MARKER_EXECUTED' not in '\n'.join(preview['logs'])
    assert preview['dry_run'] is True and preview['side_effects_blocked'] is True
    assert preview['rendered_input'] == {'n': 3} and preview['condition_branch'] is None
    result = engine.debug_step('debug-flow', 'target', allow_side_effects=True)
    assert calls == ['marker'] and 'MARKER_EXECUTED' in '\n'.join(result['logs'])
    assert result['status'] == 'succeeded' and result['output_data'] == {'value': 8}
    assert result['dry_run'] is False and result['side_effects_blocked'] is False
    assert not (tmp_path / 'data/workflow_runs').exists()
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')} == before


# LLM: 输入映射先解析再决定预览；mock覆盖同名映射但不丢弃其余上游输入。
# 函数用途: 验证默认被阻止的节点也给出完整渲染输入。
def test_debug_rendered_input_mock_and_upstream_resolution(tmp_path):
    script(tmp_path, 'mapped', 'print(\'{"value":1}\')')
    save_node(tmp_path, 'deterministic_script', script_id='mapped', inputs_map={
        'n': {'step_id': 'before', 'key': 'value'}, 'other': {'step_id': '$inputs', 'key': 'source'}})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target',
        mock_inputs={'n': 7, 'source': 9}, context_overrides={'before': {'value': 5}})
    assert result['rendered_input'] == {'n': 7, 'source': 9, 'other': 9}
    assert result['condition_branch'] is None and result['side_effects_blocked']


# LLM: 条件拒绝分支必须先于任何显式许可的脚本副作用；布尔branch不是“执行成功”的替代标签。
# 函数用途: 确认非condition节点的条件求值进入返回合同且false分支跳过执行。
@pytest.mark.parametrize('matched', [False, True])
def test_debug_step_condition_precedes_side_effect_permission(tmp_path, monkeypatch, matched):
    calls = observe_script_runs(monkeypatch)
    script(tmp_path, 'conditional', 'import sys; print("MARKER_EXECUTED", file=sys.stderr); print(\'{"value":1}\')')
    save_node(tmp_path, 'deterministic_script', script_id='conditional',
        condition={'step_id': '$inputs', 'key': 'enabled', 'equals': True})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target',
        mock_inputs={'enabled': matched}, allow_side_effects=True)
    assert result['condition_branch'] is matched
    assert calls == (['conditional'] if matched else [])
    assert ('MARKER_EXECUTED' in '\n'.join(result['logs'])) is matched
    assert result['output_data'] == ({'value': 1} if matched else {})
    assert result['status'] == ('succeeded' if matched else 'skipped')


# LLM: Store往返不能丢弃mode/config；纯节点执行不需要外部回调或显式副作用许可。
# 函数用途: 覆盖条件、纯映射和有界表达式转换。
@pytest.mark.parametrize('mode,config,inputs,expected,branch', [
    ('script_transform', {'mapping': {'value': {'step_id': '$inputs', 'key': 'n'}}}, {'n': 4}, {'value': 4}, None),
    ('script_transform', {'expression': "{'value': inputs['n'] * 2}"}, {'n': 4}, {'value': 8}, None),
    ('condition', {'expression': "inputs['n'] > 3"}, {'n': 4}, {'matched': True}, True),
    ('condition', {'expression': "inputs['n'] > 3"}, {'n': 2}, {'matched': False}, False),
    ('condition_branch', {'expression': "inputs['n'] > 3"}, {'n': 4}, {'matched': True}, True),
    ('condition_branch', {'expression': "inputs['n'] > 3"}, {'n': 2}, {'matched': False}, False),
])
def test_generic_pure_nodes_roundtrip_and_evaluate_in_memory(tmp_path, mode, config, inputs, expected, branch):
    saved = save_node(tmp_path, mode, config=config)
    reloaded = WorkflowStore(tmp_path).get_workflow('debug-flow')
    assert reloaded == saved and reloaded.steps[0].config == config
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', mock_inputs=inputs)
    assert result['status'] == 'succeeded' and result['output_data'] == expected
    assert result['condition_branch'] is branch and result['rendered_input'] == inputs
    assert not result['dry_run'] and not result['side_effects_blocked']
    assert not (tmp_path / 'data/workflow_runs').exists()


# LLM: 默认HTTP、工具和子流均不调用注入端口；执行许可不等于授予新权限，回调仍由可信宿主提供。
# 函数用途: 覆盖外部通用节点的阻止、显式执行和无账本合同。
@pytest.mark.parametrize('mode', ['http_request', 'tool_call', 'subworkflow'])
def test_generic_effect_nodes_require_explicit_true_and_trusted_executor(tmp_path, mode):
    save_node(tmp_path, mode, config={'target': 'fixture'})
    seen = []
    # LLM: 仅记录可信依赖调用，不执行网络或嵌套工作流，不能将组件边界当真实外部验收。
    # 函数用途: 验证执行许可、输入与超时确实传至唯一注入端口。
    def execute(spec, context, *, timeout_seconds):
        seen.append((spec['mode'], context.step_inputs, timeout_seconds))
        return {'value': 'executed'}
    engine = WorkflowEngine(tmp_path, debug_node_handler=execute)
    preview = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 1})
    assert not seen and preview['dry_run'] and preview['side_effects_blocked']
    result = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 1}, allow_side_effects=True)
    assert seen == [(mode, {'n': 1}, 30.0)]
    assert result['status'] == 'succeeded' and result['output_data'] == {'value': 'executed'}
    assert not (tmp_path / 'data/workflow_runs').exists()


# LLM: 没有执行器不能把dry-run当作真实HTTP/工具/子流完成。
# 函数用途: 明确显式许可仍缺可信外部依赖时的失败事实。
@pytest.mark.parametrize('mode', ['http_request', 'tool_call', 'subworkflow'])
def test_generic_effect_nodes_without_adapter_report_unavailable(tmp_path, mode):
    save_node(tmp_path, mode)
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', allow_side_effects=True)
    assert result['status'] == 'failed'
    assert result['error_detail']['code'] == 'NODE_EXECUTOR_UNAVAILABLE'
    assert not result['dry_run'] and not result['side_effects_blocked']


# LLM: 默认不调用可能有外部副作用的任意分析器；受控mock结果只作为preview且明确未调用LLM。
# 函数用途: 核验LLM节点不会用“纯节点”名义绕过副作用门。
@pytest.mark.parametrize('mode', ['llm_prompt', 'llm_skill_analysis'])
def test_debug_analyzer_is_not_called_without_explicit_permission(tmp_path, mode):
    save_node(tmp_path, mode)
    seen = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: seen.append(args) or {'value': 7})
    preview = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 2})
    assert not seen and preview['side_effects_blocked'] and preview['dry_run']
    result = engine.debug_step('debug-flow', 'target', allow_side_effects=True)
    assert len(seen) == 1 and result['output_data'] == {'value': 7}


# LLM: 表达式不能调用、取属性或消耗无界算力；失败合同仍须包含输入和条件字段。
# 函数用途: 防止转换节点变成eval、shell或资源炸弹入口。
@pytest.mark.parametrize('expression', ["__import__('os').system('echo bad')", "inputs.__class__", "'a' * 1000000000", '2 ** 999999999'])
def test_transform_expression_cannot_execute_code_or_allocate_unbounded_output(tmp_path, expression):
    save_node(tmp_path, 'script_transform', config={'expression': expression})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', mock_inputs={'n': 1})
    assert result['status'] == 'failed' and result['rendered_input'] == {'n': 1}
    assert result['condition_branch'] is None


# LLM: Python直调也要严格布尔，不允许字符串false或1获得代码执行权。
# 函数用途: 验证权限参数在任何IO/脚本前拒绝类型混淆。
@pytest.mark.parametrize('value', ['false', 1, None, {}])
def test_debug_allow_side_effects_is_strict_boolean(tmp_path, value):
    with pytest.raises(ValueError, match='allow_side_effects'):
        WorkflowEngine(tmp_path).debug_step('missing', 'target', allow_side_effects=value)


# LLM: 独立检查公开类型字段，不用dict默认值掩盖漏接字段。
# 函数用途: 保证Python DTO和HTTP字典合同同源。
def test_step_debug_result_includes_rendered_input_and_condition_branch():
    from agent_py_agent.agent.workflows import models
    result_type = getattr(models, 'StepDebugResult', None)
    assert result_type is not None
    names = {item.name for item in fields(result_type)}
    assert {'rendered_input', 'condition_branch', 'dry_run', 'side_effects_blocked'} <= names


# LLM: 原脚本错误上下文替换不能抹掉已经计算的调试分支；失败仍须返回完整合同。
# 函数用途: 锁定真实失败脚本上的condition_branch，而非只测成功路径。
def test_debug_failed_script_retains_evaluated_condition(tmp_path):
    script(tmp_path, 'bad', 'raise RuntimeError("fixture")')
    save_node(tmp_path, 'deterministic_script', script_id='bad',
        condition={'step_id': '$inputs', 'key': 'enabled', 'equals': True})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target',
        mock_inputs={'enabled': True}, allow_side_effects=True)
    assert result['status'] == 'failed' and result['condition_branch'] is True
    assert result['rendered_input'] == {'enabled': True}


# LLM: 只观察乘法是否提前分配，不真的创建资源炸弹；输入已超过纯表达式复制预算时必须在运算前拒绝。
# 函数用途: 防止嵌套容器复制按顶层len估算而放大内存。
def test_transform_nested_size_budget_is_checked_before_replication(monkeypatch):
    import ast

    from agent_py_agent.agent.workflows import debug_nodes
    seen = []
    original = debug_nodes._BINARY[ast.Mult]
    # LLM: 受控观察真实算子，不替换结果，测试输入只产生约96KiB数据。
    # 函数用途: 检查超限复制是否先被预算门挡住。
    def multiply(left, right):
        seen.append(True)
        return original(left, right)
    monkeypatch.setitem(debug_nodes._BINARY, ast.Mult, multiply)
    with pytest.raises(ValueError):
        debug_nodes.evaluate_expression("inputs['items'] * 32", {'items': ['a' * 3000]})
    assert not seen


# LLM: 通用转换引用脚本时仍须传原冻结工作流数据源，不能因新模式丢失原执行合同。
# 函数用途: 验证显式执行的script_transform与deterministic_script读取相同的工作流覆盖。
def test_debug_script_transform_preserves_workflow_datasource_override(tmp_path):
    script(tmp_path, 'source', 'import os,json; print(json.dumps({"value":json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])}))')
    value = definition({'step_id': 'target', 'mode': 'script_transform', 'script_id': 'source'})
    sources = [{'source_id': 'fixture', 'system': 'logs', 'query_scope': {'scope': 'workflow'}, 'evaluated_candidates': []}]
    value['descriptor']['data_sources'] = sources
    WorkflowStore(tmp_path).save_workflow(value)
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', allow_side_effects=True)
    assert result['status'] == 'succeeded' and result['output_data'] == {'value': sources}
    assert result['rendered_input'] == {} and result['condition_branch'] is None


# LLM: 本轮扩展的是单节点调试，不是正式DAG新执行权；原正式run不得把未知mode当可运行脚本/分析。
# 函数用途: 防止可存储通用节点意外绕过正式摘要/执行策略。
@pytest.mark.parametrize('mode', ['script_transform', 'llm_prompt', 'condition_branch', 'approval_wait', 'notification', 'script'])
def test_debug_generic_modes_do_not_implicitly_enable_formal_execution(tmp_path, mode):
    marker = tmp_path / 'formal-marker'
    script(tmp_path, 'formal', f'from pathlib import Path; Path({str(marker)!r}).write_text("bad"); print(\'{{"value":1}}\')')
    save_node(tmp_path, mode, script_id='formal' if mode in ('script_transform', 'script') else '')
    seen = []
    run = WorkflowEngine(tmp_path, analysis_handler=lambda *args: seen.append(args) or {'value': 1}).run_workflow('debug-flow')
    assert run.status == 'failed' and not seen and not marker.exists()
    assert run.steps[0].error_code == 'NODE_EXECUTOR_UNAVAILABLE'


# LLM: 前置执行条件和condition节点自身表达式不是一个缓存，不能用True前置条件掩盖False或坏表达式。
# 函数用途: 验证两层条件分别生效且关闭的前置门不求值节点正文。
@pytest.mark.parametrize('mode', ['condition', 'condition_branch'])
@pytest.mark.parametrize('gate,expression,status,branch', [
    (True, 'False', 'succeeded', False),
    (True, "__import__('os')", 'failed', True),
    (False, "__import__('os')", 'skipped', False),
])
def test_condition_precondition_does_not_replace_node_expression(tmp_path, mode, gate, expression, status, branch):
    save_node(tmp_path, mode, config={'expression': expression},
        condition={'step_id': '$inputs', 'key': 'gate', 'equals': True})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', mock_inputs={'gate': gate})
    assert result['status'] == status and result['condition_branch'] is branch
    if status == 'succeeded':
        assert result['output_data'] == {'matched': False}


# LLM: 即便可信注入者误标side_effect_free，也不能在默认预览里执行任意外部分析器；例外只允许固定内置mock。
# 函数用途: 防止分析器展示属性变成副作用门的免检通行证。
def test_arbitrary_analyzer_purity_attribute_cannot_bypass_debug_default(tmp_path):
    save_node(tmp_path, 'llm_prompt')
    seen = []
    # LLM: 受控替身故意谎报纯属性，只记录调用事实，不访问外部。
    # 函数用途: 模拟错误声明的外部分析器，检测默认debug是否仍调用它。
    def analyze(*args):
        seen.append(True)
        return {'value': 1}
    analyze.side_effect_free = True
    result = WorkflowEngine(tmp_path, analysis_handler=analyze).debug_step('debug-flow', 'target')
    assert result['side_effects_blocked'] and not seen


# LLM: 别名从既有值投影，不建立第二份状态；成功/失败/跳过/抑制均保留相同键。
# 函数用途: 检查单步结果的新旧字段及未持久化声明。
def assert_debug_contract(result, mode):
    assert {'step_type', 'output', 'error', 'side_effects_suppressed', 'persisted'} <= result.keys()
    assert result['step_type'] == mode
    assert result['output'] == result['output_data']
    assert result['error'] == result['error_detail']
    assert result['side_effects_suppressed'] is bool(result['side_effects_blocked'])
    assert result['persisted'] is False


# LLM: 成功、失败、条件跳过及副作用抑制共用同源DTO断言；仅使用临时定义，不创建正式run。
# 函数用途: 覆盖所有调试结果分支，防止新旧字段只在成功时齐全。
@pytest.mark.parametrize('scenario', ['success', 'failed', 'skipped', 'suppressed'])
def test_debug_contract_aliases_cover_all_result_branches(tmp_path, scenario):
    mode = 'deterministic_script' if scenario in ('skipped', 'suppressed') else 'script_transform'
    options = {'condition': {'step_id': '$inputs', 'key': 'gate', 'equals': True}} if scenario == 'skipped' else {}
    save_node(tmp_path, mode, config={'expression': "inputs['missing']" if scenario == 'failed' else "{'value': 4}"}, **options)
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', mock_inputs={'gate': False})
    assert result['status'] == {'failed': 'failed', 'skipped': 'skipped'}.get(scenario, 'succeeded')
    assert_debug_contract(result, mode)


# LLM: 使用合成Bearer值检查两份别名均脱敏；修改返回投影不得污染原DTO或另一输出快照。
# 函数用途: 验证调试字典的脱敏一致性及可变对象隔离，不接触真实凭据。
def test_debug_dto_aliases_share_redaction_but_not_mutable_state():
    from agent_py_agent.agent.workflows.models import StepDebugResult
    dto = StepDebugResult('flow', 'step', 'failed', 1, {'value': 'Bearer fixture-token'}, {},
        error_detail={'code': 'FIXTURE', 'message': 'Bearer fixture-error'}, side_effects_blocked=True)
    dto.step_type = 'script'
    result = dto.to_dict()
    assert_debug_contract(result, 'script')
    assert 'fixture-token' not in json.dumps(result) and 'fixture-error' not in json.dumps(result)
    result['output']['value'] = 'changed'
    assert result['output_data'] != result['output'] and dto.output_data['value'] == 'Bearer fixture-token'


# LLM: 审批不可自批，通知仅核临时Owner本地账/no_channel；显式副作用许可不允许写正式run或资产摘要。
# 函数用途: 对比控制节点默认预览与显式执行，检查真实通知记录及正式账本零变更。
@pytest.mark.parametrize('mode', ['approval_wait', 'notification'])
def test_debug_control_nodes_preview_then_execute_without_formal_ledger(tmp_path, monkeypatch, mode):
    save_node(tmp_path, mode, config={'title': 'fixture', 'message': 'notice'})
    engine = WorkflowEngine(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')}
    notices = tmp_path / 'workspace/runtime/notifications/im_alerts.jsonl'

    # LLM: 正式运行或摘要写端被调用即令测试失败；不替换节点执行或通知返回值来制造零写入结果。
    # 函数用途: 捕获调试控制节点意外落盘正式运行或资产摘要的行为。
    def forbidden(*args, **kwargs):
        pytest.fail('debug控制节点不能写正式运行或资产摘要')

    for method in ('create_run', 'save_run', 'save_step_result'):
        monkeypatch.setattr(engine.store, method, forbidden)
    monkeypatch.setattr(WorkflowStore, 'record_run_summary', forbidden)
    preview = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 2, 'approved': True})
    assert_debug_contract(preview, mode)
    assert preview['dry_run'] and preview['side_effects_suppressed'] and preview['output'] == {}
    assert preview['rendered_input'] == {'n': 2, 'approved': True} and not notices.exists()
    result = engine.debug_step('debug-flow', 'target', mock_inputs={'n': 2, 'approved': True}, allow_side_effects=True)
    assert_debug_contract(result, mode)
    assert result['status'] == 'succeeded' and not result['dry_run'] and not result['side_effects_suppressed']
    if mode == 'approval_wait':
        assert result['output']['state'] == 'waiting_for_approval' and result['output']['approved'] is False
        assert not notices.exists()
    else:
        assert result['output']['dispatch_result']['recorded'] is True
        assert result['output']['dispatch_result']['delivery_status'] == 'no_channel'
        rows = [json.loads(line) for line in notices.read_text().splitlines()]
        assert len(rows) == 1 and rows[0]['message'] == 'notice' and rows[0]['payload'] == {'n': 2, 'approved': True}
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*.json')} == before
    assert not (tmp_path / 'data/workflow_runs').exists()


# LLM: 前置条件False必须先于任意副作用许可生效；脚本零调用、通知零记录，结果仍保留完整调试合同。
# 函数用途: 验证新增副作用节点在两种许可开关下都会被关闭的前置条件跳过。
@pytest.mark.parametrize('mode', ['approval_wait', 'notification', 'script'])
@pytest.mark.parametrize('allow', [False, True])
def test_debug_new_effect_modes_respect_false_precondition(tmp_path, monkeypatch, mode, allow):
    calls = observe_script_runs(monkeypatch)
    save_node(tmp_path, mode, condition={'step_id': '$inputs', 'key': 'gate', 'equals': True})
    result = WorkflowEngine(tmp_path).debug_step('debug-flow', 'target', mock_inputs={'gate': False}, allow_side_effects=allow)
    assert result['status'] == 'skipped' and result['condition_branch'] is False and result['output_data'] == {}
    assert not calls and not (tmp_path / 'workspace/runtime/notifications/im_alerts.jsonl').exists()
    assert_debug_contract(result, mode)
