# LLM: W1契约回归只使用临时Owner、真实存储和合成上游；进程行为测试不冒称原生沙箱或IM外发已验证。
# 模块用途: 覆盖脚本源差异、CAS/workspace、能力包目录、工作流断点与身份隐私协议。
import importlib
import json
import sqlite3
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, DataSourceSpec
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows import datasource_runtime as runtime


# LLM: 断言接口存在，缺实现不能被ImportError或skip冒充红绿验证。
# 函数用途: 获取本轮新增接口。
def api(module, name):
    function = getattr(module, name, None)
    assert callable(function), name
    return function


# LLM: 正式Store写合成脚本，不依赖真实用户目录或模型服务。
# 函数用途: 建立一条可带源绑定的脚本。
def save_script(home, name='collect', sources=(), code='print("{\\"value\\": 1}")', **options):
    return ScriptStore(home, workspace_root=options.pop('workspace_root', None)).save_script(
        AssetDescriptor(name, 'script', '采集', summary='汇总', data_sources=tuple(sources)), code, **options)


# LLM: 源名称写在options而非扩展AssetDescriptor；文件只位于测试Owner。
# 函数用途: 登记一个真实本地日志数据源。
def file_source(home, name='traffic', content='record\n', **options):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    home.mkdir(parents=True, exist_ok=True)
    path = home / (name + '.log')
    path.write_text(content)
    DatasourceStore(home).save_datasource({'datasource_id': name, 'kind': 'file', 'title': name,
        'options': {'path': str(path), 'device_name': 'fw-01', 'log_source_name': name + '.log', 'system': 'logs', **options}})
    return DataSourceSpec(name, 'logs', query_scope={'device_name': 'fw-01', 'log_source_name': name + '.log'})


# LLM: 公共展示固定字段不得回写正式描述符；普通工具无源不探测连接。
# 函数用途: 验证get/list统一四字段且可继续更新摘要。
def test_script_fixed_fields_and_descriptor_roundtrip(tmp_path):
    row = save_script(tmp_path)
    assert row.get('script_name') == '采集'
    assert row.get('device_name') == row.get('log_source_name') == ''
    assert row['source_bindings'] == [] and row['source_check']['status'] == 'not_applicable'
    assert ScriptStore(tmp_path).list_scripts()[0]['script_name'] == '采集'
    raw = json.loads((tmp_path / 'scripts/collect/script.json').read_text())
    assert 'source_check' not in raw and 'script_name' not in raw
    ScriptStore(tmp_path).record_run_summary('collect', 'succeeded', 1)
    assert ScriptStore(tmp_path).get_script('collect')['runtime_summary']['run_count'] == 1


# LLM: 断言实际差集和优先级，不把列表为空当作来源无数据。
# 函数用途: 覆盖缺失、禁用、空文件、同设备新增及正常源。
@pytest.mark.parametrize('state', ['ok', 'source_missing', 'disabled', 'source_empty', 'source_added'])
def test_source_check_real_registry_and_file(tmp_path, state):
    source = DataSourceSpec('absent', 'logs') if state == 'source_missing' else file_source(
        tmp_path, content='' if state == 'source_empty' else 'record\n', enabled=state != 'disabled')
    if state == 'source_added':
        file_source(tmp_path, 'threat')
    descriptor = AssetDescriptor('collect', 'script', '采集', data_sources=(source,))
    bindings, check = api(runtime, 'evaluate_script_source_check')(tmp_path, descriptor)
    assert bindings[0]['source_id'] == source.source_id
    assert check['status'] == ('source_missing' if state == 'disabled' else state)
    assert check['checked_at']
    key = {'source_missing': 'missing_sources', 'disabled': 'missing_sources',
           'source_empty': 'empty_sources', 'source_added': 'added_sources'}.get(state)
    if key:
        assert check[key]


# LLM: SELECT由只读SDK执行，空结果不以SQLite文件非零大小掩盖。
# 函数用途: 验证显式查询返回零行也会被识别为空源。
def test_source_check_empty_sql_query(tmp_path):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    path = tmp_path / 'events.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE events(id INTEGER)')
    DatasourceStore(tmp_path).save_datasource({'datasource_id': 'events', 'kind': 'sqlite', 'title': 'events', 'database_path': str(path)})
    source = DataSourceSpec('events', 'sqlite', query_scope={'query': 'SELECT id FROM events'})
    _, check = api(runtime, 'evaluate_script_source_check')(tmp_path, AssetDescriptor('s', 'script', 's', data_sources=(source,)))
    assert check['status'] == 'source_empty' and check['empty_sources'][0]['source_id'] == 'events'


# LLM: 缺源应在运行入口前拦截，持久告警不等于已送达外部IM。
# 函数用途: 证明脚本失败不执行用户代码且告警包含准确源信息。
def test_script_source_guard_stops_before_execution(tmp_path):
    save_script(tmp_path, sources=(DataSourceSpec('missing', 'logs'),), code='raise AssertionError("must not run")')
    tool = ScriptsTool(tmp_path)
    result = tool.execute({'action': 'run', 'script_id': 'collect'})
    assert not result.ok
    data = json.loads(result.output)
    assert data['error_code'] == 'DATASOURCE_MISSING' and data['needs_repair']
    assert data['source_check']['status'] == 'source_missing'
    notices = [json.loads(line) for line in (tmp_path / 'workspace/runtime/notifications/im_alerts.jsonl').read_text().splitlines()]
    assert notices[-1]['event_type'] == 'source_missing'
    checked = tool.execute({'action': 'check_sources', 'script_id': 'collect'})
    assert checked.ok and json.loads(checked.output)['source_check']['status'] == 'source_missing'


# LLM: stale CAS必须保持原代码，错误码不能只埋在自由文本里。
# 函数用途: 覆盖ScriptsTool与WorkflowsTool的公开CAS透传。
def test_tools_revision_conflict_is_structured(tmp_path):
    row = save_script(tmp_path)
    result = ScriptsTool(tmp_path).execute({'action': 'update', 'script_id': 'collect', 'code': 'changed', 'expected_revision': row['revision'] + 1})
    assert not result.ok and json.loads(result.output)['error_code'] == 'ASSET_REVISION_CONFLICT'
    assert ScriptStore(tmp_path).get_script('collect')['code'] != 'changed'
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), ())
    saved = WorkflowStore(tmp_path).save_workflow(definition)
    result = WorkflowsTool(tmp_path).execute({'action': 'save', 'workflow_id': 'daily', 'definition': saved.to_dict(),
        'expected_revision': saved.descriptor.revision + 1})
    assert not result.ok and json.loads(result.output)['error_code'] == 'ASSET_REVISION_CONFLICT'


# LLM: 只读workspace覆盖由宿主参数提供，模型参数不能选择任意路径；真实进程测试明确不用原生沙箱。
# 函数用途: 验证项目脚本可从工具读取/执行，不串到同名Owner脚本。
def test_workspace_scripts_tools_read_and_run(tmp_path):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    home, project = tmp_path / 'owner', tmp_path / 'project'
    project.mkdir()
    save_script(home, code='print("{\\"value\\": 1}")')
    save_script(home, code='print("{\\"value\\": 2}")', workspace_root=project, target_scope='workspace')
    tool = ScriptsTool(home, workspace_root=project, runner=ScriptRunner(home, workspace_root=project, use_sandbox=False))
    assert json.loads(tool.execute({'action': 'get', 'script_id': 'collect'}).output)['scope_layer'] == 'workspace'
    result = tool.execute({'action': 'run', 'script_id': 'collect'})
    assert result.ok and json.loads(result.output)['output_data']['value'] == 2


# LLM: 用真实工作流定义和账本，不让模型回调替代最终输出合同。
# 函数用途: 检查工作流最终字段缺失及类型错误均产生结构化断点。
@pytest.mark.parametrize('output', [{}, {'report_uri': 1}])
def test_workflow_final_contract_hard_stop(tmp_path, output):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    desc = AssetDescriptor.from_dict({'id': 'daily', 'kind': 'workflow', 'name': '日报',
        'io_contract': {'outputs': ['report_uri'], 'output_types': {'report_uri': 'string'}}})
    definition = WorkflowDefinition(desc, (WorkflowStepSpec('judge', '研判', 'llm_skill_analysis'),))
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: output)
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'failed'
    assert result.breakpoint['error_code'] == 'WORKFLOW_OUTPUT_CONTRACT_VIOLATION'
    assert result.workflow_name == '日报' and result.total_steps == result.completed_steps == 1
    assert engine.store.load_run(result.workflow_run_id).breakpoint == result.breakpoint


# LLM: 从原pending步恢复必须核对尚未执行的依赖；明确允许漂移才能采用新版本。
# 函数用途: 验证暂停、脚本版本冻结、默认拦截和显式放行后的前缀保留。
def test_workflow_resume_drift_gate_and_breakpoint(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('prefix', '前序', 'llm_skill_analysis'),
        WorkflowStepSpec('confirm', '确认', 'llm_skill_analysis'),
        WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect')))
    pauses = []
    # LLM: 确认首次暂停、重跑后完成；前序是独立成功步骤，不把暂停误当成功前缀。
    # 函数用途: 模拟一次确认暂停的分析器。
    def analyze(spec, *_):
        if spec['step_id'] == 'confirm' and not pauses:
            pauses.append(True)
            return {'_pause_for_resume': True}
        return {'approved': True}
    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    paused = engine.run_workflow(definition.to_dict())
    assert getattr(paused, 'breakpoint', None) and paused.breakpoint['step_index'] == 2
    frozen = engine.store.load_workflow_snapshot(paused.workflow_run_id)['_frozen_scripts']['collect']
    assert frozen['script_id'] == 'collect' and frozen['code_sha256']
    save_script(tmp_path, code='print("{\\"value\\": 2}")')
    blocked = engine.resume_workflow_run(paused.workflow_run_id)
    assert blocked.breakpoint['error_code'] == 'SCRIPT_REVISION_DRIFT'
    assert blocked.steps[2].error_code == 'SCRIPT_REVISION_DRIFT' and blocked.steps[2].needs_repair
    assert engine.store.load_run(paused.workflow_run_id).steps[2].error_code == 'SCRIPT_REVISION_DRIFT'
    resumed = engine.resume_workflow_run(paused.workflow_run_id, allow_script_drift=True)
    assert resumed.status == 'succeeded' and resumed.final_output == {'value': 2}
    assert resumed.steps[0] == paused.steps[0]


# LLM: 数据类字段向后兼容且事件身份不构成授权，不接触真实IM。
# 函数用途: 验证actor两字段经过标准化后仍进入公共事件载荷。
def test_event_actor_fields_round_trip():
    module = importlib.import_module('agent_py_agent.agent.plugin_events.protocol')
    assert 'actor_user_ref' in module.EventFact.__dataclass_fields__
    fact = module.EventFact('prompt_submitted', actor_user_ref='feishu:ou_test', actor_display_name='测试用户')
    normalized = module.normalize_event_fact(fact)
    data = module.build_event_payload(module.EventEnvelope(normalized, 'event1', 1, 0, 1))
    assert data['actor_user_ref'] == 'feishu:ou_test' and data['actor_display_name'] == '测试用户'


# LLM: R2将不可绕过的硬门限定于源缺失、空源和版本漂移；宿主显式策略不授予绕过权限。
# 函数用途: 验证预检失败始终停在当前步，成功前缀及尾步状态准确。
@pytest.mark.parametrize('policy', ['skip', 'escalate_to_agent'])
@pytest.mark.parametrize('state,error', [('missing', 'DATASOURCE_MISSING'),
    ('empty', 'DATASOURCE_EMPTY'), ('drift', 'SCRIPT_REVISION_DRIFT')])
def test_workflow_script_hard_stop_cannot_skip_or_auto_recover(tmp_path, policy, state, error):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    sources = (DataSourceSpec('missing', 'logs'),) if state == 'missing' else (
        (file_source(tmp_path, content=''),) if state == 'empty' else ())
    save_script(tmp_path, name='broken', sources=sources)
    calls = []
    # LLM: 前序分析后修改已冻结的后续代码，漂移必须在runner前被拦截。
    # 函数用途: 为版本门构造确定性交错。
    def analyze(*_):
        if state == 'drift':
            save_script(tmp_path, name='broken', code='print("{}")')
        return {'prefix': True}
    engine = WorkflowEngine(tmp_path, analysis_handler=analyze,
        escalation_handler=lambda *args: calls.append(args) or {'recovered': True, 'output_data': {'value': 1}})
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('prefix', '前序', 'llm_skill_analysis'),
        WorkflowStepSpec('broken', '故障', 'deterministic_script', script_id='broken', on_error=policy),
        WorkflowStepSpec('tail', '尾步', 'llm_skill_analysis')))
    run = engine.run_workflow(definition.to_dict())
    assert run.status in ('failed', 'paused_for_resume')
    assert run.steps[0].output_snapshot == {'prefix': True} and run.steps[2].status == 'pending'
    assert run.breakpoint['step_id'] == 'broken' and run.breakpoint['step_index'] == 2
    assert run.completed_steps == 1 and calls == [] and run.steps[1].error_code == error


# LLM: 仅空登记表允许不含连接键的被动内联范围；并不授予SDK访问不存在连接的权限。
# 函数用途: 验证旧参数源不再误报连接缺失。
def test_r2_passive_inline_scope_without_registry(tmp_path):
    source = DataSourceSpec('apps', 'logs', query_scope={'active_appnames': ['A', 'B']})
    _, check = runtime.evaluate_script_source_check(tmp_path, AssetDescriptor('collect', 'script', '采集', data_sources=(source,)))
    assert check['status'] == 'ok' and check['missing_sources'] == []


# LLM: 空范围及任何显式连接/权限键都不属于被动参数，即使键值为空也不得回退。
# 函数用途: 保护被动范围兼容规则的严格边界。
@pytest.mark.parametrize('scope', [{}, *[{key: ''} for key in (
    'device_name', 'log_source_name', 'path', 'file_path', 'database_path', 'query', 'sql', 'access')]])
def test_r2_explicit_connection_scope_still_missing(tmp_path, scope):
    source = DataSourceSpec('apps', 'logs', query_scope=scope)
    _, check = runtime.evaluate_script_source_check(tmp_path, AssetDescriptor('collect', 'script', '采集', data_sources=(source,)))
    assert check['status'] == 'source_missing'


# LLM: 已有登记连接时，旧参数样式也不能掩盖删除或错配的源ID。
# 函数用途: 验证兼容回退仅限全空的连接目录。
def test_r2_passive_scope_with_other_registry_entry_still_missing(tmp_path):
    file_source(tmp_path)
    source = DataSourceSpec('apps', 'logs', query_scope={'active_appnames': ['A']})
    _, check = runtime.evaluate_script_source_check(tmp_path, AssetDescriptor('collect', 'script', '采集', data_sources=(source,)))
    assert check['status'] == 'source_missing' and check['missing_sources'][0]['source_id'] == 'apps'


# LLM: 展示时间只缓存状态，不缓存健康检查；显式check_sources仍实际检查且刷新时间。
# 函数用途: 验证相同读取稳定、实际源状态往返变化及显式检查刷新。
def test_r2_read_projection_timestamp_stable_but_checks_live(tmp_path):
    source = file_source(tmp_path)
    save_script(tmp_path, sources=(source,))
    tool = ScriptsTool(tmp_path)
    before = json.loads(tool.execute({'action': 'get', 'script_id': 'collect'}).output)
    again = json.loads(ScriptsTool(tmp_path).execute({'action': 'get', 'script_id': 'collect'}).output)
    assert before == again
    explicit = json.loads(tool.execute({'action': 'check_sources', 'script_id': 'collect'}).output)['source_check']
    assert explicit['checked_at'] != before['source_check']['checked_at']
    (tmp_path / 'traffic.log').write_text('')
    empty = ScriptStore(tmp_path).get_script('collect')['source_check']
    assert empty['status'] == 'source_empty' and empty['checked_at'] != before['source_check']['checked_at']
    (tmp_path / 'traffic.log').write_text('record\n')
    restored = ScriptStore(tmp_path).get_script('collect')['source_check']
    assert restored['status'] == 'ok' and restored['checked_at'] not in (empty['checked_at'], before['source_check']['checked_at'])


# LLM: 环境覆盖不扩大SDK授权；显式空列表/字典只清除本次健康预检声明，不能落盘改描述符。
# 函数用途: 验证空覆盖与旧被动dict不再沿用缺失的原源。
@pytest.mark.parametrize('overrides', [[], {}, {'active': ['new']}])
def test_r2_explicit_passive_override_clears_preflight(tmp_path, overrides):
    row = save_script(tmp_path, sources=(DataSourceSpec('missing', 'logs'),))
    check = runtime._execution_source_check(tmp_path, row, overrides, None)
    assert check['status'] == 'not_applicable' and check['missing_sources'] == []
    assert ScriptStore(tmp_path).get_script('collect')['source_check']['status'] == 'source_missing'


# LLM: 默认引擎不能把真实运行失败或缺输出当成功；错误需要保留具体修复指引。
# 函数用途: 验证默认暂停和repair_guide，不注入任何自动恢复策略。
@pytest.mark.parametrize('code,error', [('raise RuntimeError("broken")', 'SCRIPT_EXIT_NONZERO'),
    ('print("{}")', 'SCRIPT_MISSING_OUTPUT_KEYS')])
def test_r2_runtime_error_default_pauses_with_repair_guide(tmp_path, code, error):
    from agent_py_agent.agent.assets.descriptor import IOContract
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    ScriptStore(tmp_path).save_script(AssetDescriptor('broken', 'script', '故障', io_contract=IOContract(outputs=('value',))), code)
    engine = WorkflowEngine(tmp_path)
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('broken', '故障', 'deterministic_script', script_id='broken'),))
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'paused_for_resume' and result.needs_repair and result.steps[0].error_code == error
    assert result.steps[0].escalation_result['repair_guide']['error_code'] == error


# LLM: 仅显式skip/宿主注入的恢复回调可处理普通运行错误；与预检硬门区别验证。
# 函数用途: 验证合法的旧错误策略恢复兼容。
@pytest.mark.parametrize('policy', ['skip', 'escalate_to_agent'])
def test_r2_runtime_errors_obey_explicit_policy(tmp_path, policy):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path, code='raise RuntimeError("broken")')
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *_: {'done': True},
        escalation_handler=lambda *_: calls.append(True) or {'recovered': True, 'output_data': {'value': 1}})
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect', on_error=policy),
        WorkflowStepSpec('tail', '后续', 'llm_skill_analysis')))
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'succeeded' and result.final_output == {'done': True}
    assert result.steps[0].status == ('skipped' if policy == 'skip' else 'succeeded')
    assert calls == ([] if policy == 'skip' else [True])


# LLM: 最后一步保持真实输出；显式类型合同可由成功前序步骤满足，不合并跳过或失败步骤。
# 函数用途: 验证旧工作流与新强类型工作流都可跨步骤产出所需字段。
@pytest.mark.parametrize('typed', [False, True])
def test_r2_workflow_contract_uses_merged_successful_outputs(tmp_path, typed):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    desc = AssetDescriptor.from_dict({'id': 'daily', 'kind': 'workflow', 'name': '日报',
        'io_contract': {'outputs': ['value'], **({'output_types': {'value': 'integer'}} if typed else {})}})
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, *_: {'value': 1} if spec['step_id'] == 'first' else {'decision': 'alert'})
    definition = WorkflowDefinition(desc, (WorkflowStepSpec('first', '采集', 'llm_skill_analysis'),
        WorkflowStepSpec('last', '研判', 'llm_skill_analysis')))
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'succeeded' and result.final_output == {'decision': 'alert'}


# LLM: 未声明类型的旧工作流描述符可能复用脚本必填键；非空结果保持兼容，完全空输出仍校验。
# 函数用途: 区分非空旧输出与真正空工作流输出。
@pytest.mark.parametrize('output,expected', [({'decision': 'alert'}, 'succeeded'), ({}, 'failed')])
def test_r2_legacy_untyped_workflow_contract(tmp_path, output, expected):
    from agent_py_agent.agent.assets.descriptor import IOContract
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报', io_contract=IOContract(outputs=('value',))), (
        WorkflowStepSpec('last', '研判', 'llm_skill_analysis'),))
    result = WorkflowEngine(tmp_path, analysis_handler=lambda *_: output).run_workflow(definition.to_dict())
    assert result.status == expected


# LLM: 显式续跑允许修好当前故障脚本，但不批准后续脚本变化；成功前缀不得重跑。
# 函数用途: 验证修复重钉与后续漂移硬门不会互相混淆。
@pytest.mark.parametrize('change_tail', [False, True])
def test_r2_repaired_current_step_repins_only_itself(tmp_path, change_tail):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path, name='broken', code='raise RuntimeError("broken")')
    save_script(tmp_path, name='tail')
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *_: {'prefix': True})
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('prefix', '前序', 'llm_skill_analysis'),
        WorkflowStepSpec('repair', '修复', 'deterministic_script', script_id='broken'),
        WorkflowStepSpec('tail', '尾步', 'deterministic_script', script_id='tail')))
    paused = engine.run_workflow(definition.to_dict())
    assert paused.status == 'paused_for_resume' and paused.steps[1].needs_repair
    (tmp_path / 'scripts/broken/main.py').write_text('print("{}")')
    if change_tail:
        save_script(tmp_path, name='tail', code='print("{\\"value\\": 2}")')
    result = WorkflowEngine(tmp_path)
    result.runner = ScriptRunner(tmp_path, use_sandbox=False)
    resumed = result.resume_workflow_run(paused.workflow_run_id)
    assert resumed.steps[0] == paused.steps[0]
    if change_tail:
        assert resumed.status == 'paused_for_resume' and resumed.breakpoint['step_id'] == 'tail'
        assert resumed.breakpoint['error_code'] == 'SCRIPT_REVISION_DRIFT'
    else:
        assert resumed.status == 'succeeded'


# LLM: SDK可信预载必须保留签名冻结，但不能无关加载整个工作流/技能/归档服务；公开类的身份和导入方式保持。
# 函数用途: 在新解释器验证轻量SDK启动和所有旧公开导出。
def test_r2_sdk_preload_does_not_eagerly_import_engine():
    import subprocess
    import sys
    from pathlib import Path
    code = '''import sys
sys.path.insert(0, ROOT)
import agent_py_agent.runtime_sdk
assert 'agent_py_agent.agent.workflows.engine' not in sys.modules
assert 'agent_py_agent.agent.workflows.script_runner' not in sys.modules
assert 'agent_py_agent.agent.runtime_errors' not in sys.modules
import agent_py_agent.agent.workflows as public
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import ScriptExecutionResult, StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
for name in public.__all__:
    assert getattr(public, name) is locals()[name]
try:
    public.not_an_api
except AttributeError:
    pass
else:
    raise AssertionError('unknown export must raise AttributeError')
'''.replace('ROOT', repr(str(Path(__file__).resolve().parents[2])))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


# LLM: 延迟的错误格式化仍调用唯一原实现，异常身份、上下文和返回对象原样保留。
# 函数用途: 验证轻量导入不会复制或改写错误协议。
def test_r2_json_error_report_delegates_exact_arguments(monkeypatch):
    from agent_py_agent.agent import runtime_errors
    from agent_py_agent.agent.common import json_io
    seen, expected = [], {'original_report': True}
    # LLM: 替换原格式化口只观察参数，不替换实际JSON读写或安全校验。
    # 函数用途: 识别是否确实转交唯一错误实现。
    def report(exc, *, context=''):
        seen.append((exc, context))
        return expected
    monkeypatch.setattr(runtime_errors, 'runtime_error_report', report)
    error = ValueError('synthetic')
    assert json_io.runtime_error_report(error, context='sdk-test') is expected
    assert seen == [(error, 'sdk-test')]


# LLM: 脚本已编辑也不能让非法修复输出在验证前改变冻结pin、账本或旁件。
# 函数用途: 验证无效恢复请求在任何持久化之前失败。
def test_r2_invalid_patch_after_edit_has_no_persistent_effect(tmp_path):
    from agent_py_agent.agent.assets.descriptor import IOContract
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    ScriptStore(tmp_path).save_script(AssetDescriptor('broken', 'script', '故障', io_contract=IOContract(outputs=('value',))),
        'raise RuntimeError("broken")')
    engine = WorkflowEngine(tmp_path)
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('broken', '故障', 'deterministic_script', script_id='broken'),))
    run = engine.run_workflow(definition.to_dict())
    folder = engine.store.run_dir(run.workflow_run_id)
    before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    (tmp_path / 'scripts/broken/main.py').write_text('print("{\\"value\\": 2}")')
    with pytest.raises(ValueError, match='SCRIPT_MISSING_OUTPUT_KEYS'):
        engine.resume_workflow_run(run.workflow_run_id, patched_step_output={})
    after = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    assert before == after


# LLM: 真实脚本退出后注入合成的宿主清理上下文，仅验证合并边界，不冒称真实清理故障已复现。
# 函数用途: 防止恢复回调覆盖原修复指引或重新带回未脱敏的清理字段。
@pytest.mark.parametrize('field', ['repair_guide', 'cleanup_error'])
def test_r2_callback_cannot_overwrite_host_repair_context(tmp_path, field):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path, code='raise RuntimeError("broken")')
    # LLM: 子进程照常执行；合成附加字段不授执行权，只定位恢复结果的合并和脱敏顺序。
    # 类用途: 在真实失败之后补入一个用于测试的清理诊断。
    class ContextRunner(ScriptRunner):
        # LLM: 原始失败、退出码和输出不变；测试秘密仅为合成占位值。
        # 函数用途: 给故障结果补入明确不可被回调抹掉的上下文。
        def run_script(self, *args, **kwargs):
            result = super().run_script(*args, **kwargs)
            result.escalation_context['cleanup_error'] = {'error_code': 'SCRIPT_CLEANUP_ERROR',
                'group_cleanup_verified': False, 'password': 'synthetic-test-secret'}
            return result
    engine = WorkflowEngine(tmp_path, escalation_handler=lambda *_: {
        'recovered': False, 'repair_guide': None, 'cleanup_error': {'group_cleanup_verified': True}})
    engine.runner = ContextRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('daily', 'workflow', '日报'), (
        WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect'),))
    run = engine.run_workflow(definition.to_dict())
    assert run.status == 'paused_for_resume' and run.needs_repair
    context = run.steps[0].escalation_result
    if field == 'repair_guide':
        assert isinstance(context[field], dict) and context[field]['script_id'] == 'collect'
    else:
        assert context[field]['group_cleanup_verified'] is False
        assert 'synthetic-test-secret' not in json.dumps(context)


# LLM: 旧清单规范字节保持不变；新范围必须严格解析且不能污染调用方原字典。
# 函数用途: 保护application_scope往返及旧包摘要确定性。
def test_capability_scope_roundtrip_preserves_legacy_payload():
    from agent_py_agent.agent.capability_package_manifest import CapabilityDeclaration
    old = {'description': '能力', 'keywords': ['领域'], 'entry_document': 'CAPABILITY.md'}
    assert CapabilityDeclaration.from_payload(old).to_payload() == old
    assert 'application_scope' in CapabilityDeclaration.__dataclass_fields__
    assert replace(CapabilityDeclaration.from_payload(old), application_scope={}).to_payload() == old
    scoped = {**old, 'application_scope': {'workbenches': ['security'], 'domains': ['pentest'], 'workspace_tags': ['redteam']}}
    model = CapabilityDeclaration.from_payload(scoped)
    assert model.to_payload() == scoped
    projected = model.to_payload()
    projected['application_scope']['domains'].append('changed')
    assert model.to_payload() == scoped
    with pytest.raises(ValueError):
        CapabilityDeclaration.from_payload({**old, 'application_scope': {'domains': 'invalid'}})


# LLM: 用真实临时安装表驱动安装/停用/卸载事实，测试不启动插件或实际模型。
# 函数用途: 验证总表与唯一安装记录自动同步，卡片范围和资源清单可公开读取。
def test_capability_registry_real_install_disable_uninstall(tmp_path):
    from agent_py_agent.agent.capability import package_provider as provider
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.tests.test_capability_activation import content_activation_fixture
    listing = api(provider, 'list_installed_capability_packages')
    owner, store, _, request = content_activation_fixture(tmp_path)
    before = listing(owner.home_dir)
    assert len(before) == 1 and before[0]['enabled'] is False
    active = store.change_activation(request).installation
    rows = listing(owner.home_dir, include_disabled=False)
    assert len(rows) == 1 and rows[0]['enabled'] is True
    assert {'package_id', 'name', 'version', 'summary', 'installation_scope', 'application_scope',
            'assets', 'asset_counts', 'digest', 'revision'} <= rows[0].keys()
    snapshot = provider.enabled_capability_packages(owner)[0]
    assert api(snapshot, 'to_card')()['application_scope'] == {}
    assert api(snapshot, 'to_detail')()['assets']['skills']
    revoked = store.change_activation(PluginActivationRequest('disable', active.revision,
        replace(active.activation, phase='revoked'))).installation
    assert listing(owner.home_dir, include_disabled=False) == []
    assert listing(owner.home_dir)[0]['enabled'] is False
    released, _ = store.release_activation(owner, None, revoked, 'disable')
    store.remove('w1-remove', 'story-content', released.installation)
    assert listing(owner.home_dir) == []


# LLM: 排序仅软偏好，不执行包、不扩大授权；稳定顺序及禁用记录不得抢首位。
# 函数用途: 验证workbench/domain/tag和显式偏好四类上下文提升。
@pytest.mark.parametrize('context', [dict(workbench_id='security'), dict(domain='pentest'),
    dict(workspace_tags=['redteam']), dict(preferred_pack_ids=['target'])])
def test_package_context_ranking(context):
    from agent_py_agent.agent.capability import package_provider as provider
    packages = [{'package_id': 'plain', 'enabled': True, 'application_scope': {}},
        {'package_id': 'disabled', 'enabled': False, 'application_scope': {'workbenches': ['security']}},
        {'package_id': 'target', 'enabled': True, 'application_scope': {'workbenches': ['security'],
            'domains': ['pentest'], 'workspace_tags': ['redteam']}}]
    ranked = api(provider, 'rank_packages_for_context')(packages, **context)
    assert ranked[0]['package_id'] == 'target'
    assert packages[0]['package_id'] == 'plain'


# LLM: 合成远端请求并发执行，显示名之外不返回任何飞书资料；缓存隔离Owner及调用凭据。
# 函数用途: 验证用户/群解析、30分钟缓存、singleflight和隐私固定字段。
def test_identity_batch_cache_singleflight_and_privacy(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.adapter import feishu_profile as profile
    resolve = api(profile, 'resolve_user_space_identity')
    calls = []
    # LLM: 只模拟显示名端口，不连接飞书或记录真实凭据。
    # 函数用途: 用短暂等待制造并发重叠，观察去重次数。
    def fetch(*args):
        calls.append(args[2])
        time.sleep(0.03)
        return '测试用户'
    monkeypatch.setattr(profile, 'fetch_feishu_display_name', fetch)
    monkeypatch.setattr(profile, 'fetch_feishu_chat_name', lambda *args: '测试群')
    credentials = {'app_id': 'synthetic-app', 'app_secret': 'synthetic-secret'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: resolve(tmp_path, id_value='ou_same', feishu_credentials=credentials), range(8)))
    assert calls == ['ou_same'] and all(result['display_name'] == '测试用户' for result in results)
    assert set(results[0]) == {'id', 'id_type', 'source', 'resolved', 'display_name', 'reason'}
    resolve(tmp_path, id_value='ou_same', feishu_credentials=credentials)
    assert calls == ['ou_same']
    batch = resolve(tmp_path, targets=[{'id': 'ou_same', 'id_type': 'user'}, {'id': 'oc_group', 'id_type': 'chat'}], feishu_credentials=credentials)
    items = batch['items'] if isinstance(batch, dict) else batch
    assert [item['display_name'] for item in items] == ['测试用户', '测试群']
    assert getattr(profile, '_IDENTITY_TTL_SECONDS', 0) >= 1800
    assert 'synthetic-secret' not in json.dumps(results)


# LLM: 本地称呼只用于本人默认目标，不能替任意未知ID伪造名称；坏网络或返回值统一fail-open。
# 函数用途: 覆盖默认本人、陌生目标和认证/网络失败。
def test_identity_local_default_and_failure(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import feishu_profile as profile
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    resolve = api(profile, 'resolve_user_space_identity')
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    (owner.home_dir / 'USER.md').write_text('# User\n- 称呼：哥哥\n')
    assert resolve(owner.home_dir)['display_name'] == '哥哥'
    assert resolve(owner.home_dir, id_value='someone-else')['resolved'] is False
    # LLM: 抛出的模拟异常中带秘密，失败结果必须只公开稳定原因。
    # 函数用途: 验证异常内容不回显。
    def fail(*args):
        raise RuntimeError('do not expose secret')
    monkeypatch.setattr(profile, 'fetch_feishu_display_name', fail)
    result = resolve(owner.home_dir, id_value='ou_failure', feishu_credentials={'app_id': 'a', 'app_secret': 'b'})
    assert result['resolved'] is False and result['display_name'] is None
    assert 'secret' not in json.dumps(result)


# LLM: Broker经真实旧身份范围门，只读新动作不接收认证字段；新增主题不得仅列名未接线。
# 函数用途: 核对能力包/身份路由及脚本固定字段真正进入Broker返回值。
def test_broker_new_read_topics_and_script_projection(tmp_path):
    from agent_py_agent.agent.gateway_parts.plugin_broker_api import PluginBrokerService
    from agent_py_agent.agent.plugin_host_broker_topics import TOPIC_ACTIONS
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    owner = resolve_owner_home(tmp_path)
    save_script(owner.home_dir)
    service = PluginBrokerService(owner.home_dir, owner_id=owner.owner_id)
    assert TOPIC_ACTIONS.get('capability_packages') == 'capability_packages.list'
    assert TOPIC_ACTIONS.get('identity') == 'user_space.identity.resolve'
    packages = service.dispatch('capability_packages.list')
    assert packages['ok'] and packages['data'] == []
    identity = service.dispatch('user_space.identity.resolve', {'id': 'ou_unknown'})
    assert identity['ok'] and identity['data']['resolved'] is False
    assert not service.dispatch('user_space.identity.resolve', {'feishu_credentials': {'app_secret': 'unsafe'}})['ok']
    assets = service.dispatch('assets.list', {'kind': 'script'})
    assert assets['ok'] and assets['data'][0]['script_name'] == '采集'
    assert assets['data'][0]['device_name'] == assets['data'][0]['log_source_name'] == ''


# LLM: 同一脚本正常运行的摘要revision不是用户改代码；暂停后仍需区分真实修改，不能误报自己的计数写入。
# 函数用途: 验证前后两步复用同一脚本时可以在未修改代码的情况下恢复。
def test_workflow_resume_ignores_own_summary_revision(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path)
    pauses = []
    # LLM: 只在首次确认时暂停，第二次确认后放行，不模拟真实模型验收。
    # 函数用途: 构造一次中途暂停。
    def analyze(*_):
        if not pauses:
            pauses.append(True)
            return {'_pause_for_resume': True}
        return {'approved': True}
    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    engine.runner = ScriptRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('repeat', 'workflow', '重复采集'), (
        WorkflowStepSpec('first', '第一次', 'deterministic_script', script_id='collect'),
        WorkflowStepSpec('confirm', '确认', 'llm_skill_analysis'),
        WorkflowStepSpec('second', '第二次', 'deterministic_script', script_id='collect')))
    paused = engine.run_workflow(definition.to_dict())
    assert paused.status == 'paused_for_resume' and paused.completed_steps == 1
    resumed = engine.resume_workflow_run(paused.workflow_run_id)
    assert resumed.status == 'succeeded' and resumed.steps[0].to_dict() == paused.steps[0].to_dict()


# LLM: 实际HTTP解码面对畸形name对象时不得str化回显其它资料；只有有限字符串能成为名称。
# 函数用途: 验证上游响应即使name类型错误也不暴露phone/email。
def test_identity_invalid_upstream_name_never_stringifies_profile(monkeypatch):
    from contextlib import contextmanager
    from io import BytesIO

    from agent_py_agent.agent.adapter import feishu_profile as profile
    responses = iter([{'tenant_access_token': 'synthetic-token'},
        {'code': 0, 'data': {'user': {'name': {'phone': 'sensitive-phone', 'email': 'sensitive-email'}}}}])
    # LLM: 内存合成响应，不执行真实HTTP或触碰认证存储。
    # 函数用途: 模拟官方两步读取格式中的畸形名称字段。
    @contextmanager
    def fake_open(*_, **__):
        yield BytesIO(json.dumps(next(responses)).encode())
    monkeypatch.setattr(profile, 'open_authenticated_request', fake_open)
    assert profile.fetch_feishu_display_name('a', 'b', 'ou_invalid') is None


# LLM: 只有省略单目标才允许本人默认值，批量空ID必须invalid，认证额外对象不应打断fail-open解析。
# 函数用途: 覆盖空目标与非JSON认证附加字段。
def test_identity_invalid_batch_and_credentials_extra(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import feishu_profile as profile
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    home = resolve_owner_home(tmp_path).home_dir
    home.mkdir(parents=True)
    (home / 'USER.md').write_text('- 称呼：本人\n')
    assert profile.resolve_user_space_identity(home, targets=[{'id': ''}])['items'][0]['resolved'] is False
    monkeypatch.setattr(profile, 'fetch_feishu_display_name', lambda *_: '远端')
    result = profile.resolve_user_space_identity(home, id_value='ou_extra',
        feishu_credentials={'app_id': 'a', 'app_secret': 'b', 'unused': object()})
    assert result['resolved'] and result['display_name'] == '远端'


# LLM: 只测可信通知回调的本地入队，不冒称外部IM送达；异Owner回调不能收到通知，故障不传播。
# 函数用途: 验证通知Owner隔离和fail-open。
def test_notification_callback_is_scoped_and_fail_open(tmp_path):
    calls = []
    with runtime.bind_asset_im_notifier(tmp_path, lambda notice: calls.append(notice)):
        first = runtime.emit_asset_im_notification(tmp_path, event_type='source_added', title='采集', message='新增源', payload={})
        other = runtime.emit_asset_im_notification(tmp_path / 'other', event_type='source_empty', title='采集', message='空源', payload={})
    assert len(calls) == 1 and first['delivery_status'] == 'queued' and other['delivery_status'] == 'no_channel'
    # LLM: 模拟已配置通道入队失败；不得影响主业务返回。
    # 函数用途: 提供会失败的可信回调。
    def broken(_):
        raise RuntimeError('notifier down')
    with runtime.bind_asset_im_notifier(tmp_path, broken):
        result = runtime.emit_asset_im_notification(tmp_path, event_type='workflow_breakpoint', title='停住', message='故障', payload={})
    assert result['recorded'] and result['delivery_status'] == 'unavailable'


# LLM: 中途真实修改后续脚本必须在runner前拦截，不能只在恢复入口核对冻结版本。
# 函数用途: 证明普通连续执行也不会运行未经本次快照批准的新代码。
@pytest.mark.parametrize('mutation', ['edit', 'delete', 'delete_code'])
def test_workflow_live_edit_stops_before_next_script(tmp_path, mutation):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path)
    calls = []
    # LLM: 保留真实runner执行，只记录是否已经进入执行口。
    # 类用途: 观察版本硬门是否先于子进程准备。
    class CountingRunner(ScriptRunner):
        # LLM: 转发原参数，不伪造脚本结果。
        # 函数用途: 记录一次真正的runner入口调用。
        def run_script(self, *args, **kwargs):
            calls.append(True)
            return super().run_script(*args, **kwargs)
    # LLM: 在前序分析期间修改后续真实资产，制造启动冻结之后的漂移。
    # 函数用途: 模拟并发编辑者提交新版本。
    def edit(*_):
        if mutation == 'edit':
            save_script(tmp_path, code='print("{\\"value\\": 2}")')
        elif mutation == 'delete':
            ScriptStore(tmp_path).delete_script('collect')
        else:
            (tmp_path / 'scripts/collect/main.py').unlink()
        return {'edited': True}
    engine = WorkflowEngine(tmp_path, analysis_handler=edit)
    engine.runner = CountingRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('live', 'workflow', '连续执行'), (
        WorkflowStepSpec('before', '前序', 'llm_skill_analysis'),
        WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect', on_error='skip')))
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'failed' and result.breakpoint['error_code'] == 'SCRIPT_REVISION_DRIFT'
    assert calls == [] and result.completed_steps == 1


# LLM: 验过的代码与实际执行必须是同一份字节，模拟校验后runner加载前的精确交错，不验证真实并发调度。
# 函数用途: 证明检查与执行之间的编辑不会替换本次已经批准的代码。
def test_workflow_check_to_load_uses_same_code_snapshot(tmp_path):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    save_script(tmp_path)
    # LLM: runner入口处提交新代码后继续真实原runner，不能替换结果或加载器。
    # 类用途: 注入检查之后、原load_script之前的确定性交错。
    class EditingRunner(ScriptRunner):
        # LLM: 修改的是测试Owner正式资产，原runner仍加载并创建真实子进程。
        # 函数用途: 让原加载器面对一个刚被编辑过的同名资产。
        def run_script(self, *args, **kwargs):
            save_script(tmp_path, code='print("{\\"value\\": 2}")')
            return super().run_script(*args, **kwargs)
    engine = WorkflowEngine(tmp_path)
    engine.runner = EditingRunner(tmp_path, use_sandbox=False)
    definition = WorkflowDefinition(AssetDescriptor('race', 'workflow', '同份代码'), (
        WorkflowStepSpec('collect', '采集', 'deterministic_script', script_id='collect'),))
    result = engine.run_workflow(definition.to_dict())
    assert result.status == 'succeeded' and result.final_output == {'value': 1}
    assert '\\"value\\": 2' in ScriptStore(tmp_path).get_script('collect')['code']


# LLM: 有效运行覆盖需要额外源健康检查，但不能借环境覆盖抹除原SDK声明或扩大原授权。
# 函数用途: 证明不会检查旧非空源却对空的新覆盖继续执行。
def test_script_override_sources_are_preflighted(tmp_path):
    source = file_source(tmp_path)
    empty = file_source(tmp_path, 'empty', content='')
    save_script(tmp_path, sources=(source,))
    calls = []
    # LLM: 到达此runner就是硬门失败，结果不参与成功判断。
    # 类用途: 断言源错误之前绝不启动执行器。
    class ForbiddenRunner:
        # LLM: 只记录越过硬门的调用，不执行用户代码。
        # 函数用途: 让缺失预检表现为明确的失败断言。
        def run_script(self, *_args, **_kwargs):
            calls.append(True)
            raise AssertionError('empty source must not execute')
    result = runtime.run_script_with_datasources(tmp_path, 'collect', runner=ForbiddenRunner(),
        override_data_sources=[empty.to_dict()])
    assert result.error_code == 'DATASOURCE_EMPTY' and calls == []


# LLM: 本地用户和群的实体类型不能被页面参数混淆，本人缺省类型从可信Owner路径推导。
# 函数用途: 验证用户/群默认解析正确且显式相反类型无法冒充成功。
@pytest.mark.parametrize('kind', ['user', 'chat'])
def test_identity_self_type_is_not_confused(tmp_path, kind):
    from agent_py_agent.agent.adapter.feishu_profile import resolve_user_space_identity
    home = tmp_path / ('owners/local/main' if kind == 'user' else 'owners/providers/feishu/groups/oc_test')
    home.mkdir(parents=True)
    (home / 'USER.md').write_text('- 称呼：本人\n')
    assert resolve_user_space_identity(home)['id_type'] == kind
    wrong = resolve_user_space_identity(home, id_type='chat' if kind == 'user' else 'user')
    assert wrong['resolved'] is False and wrong['display_name'] is None


# LLM: 没有任何匹配项的维度不应改变旧包规范载荷或摘要输入。
# 函数用途: 将全空数组范围规范化为缺省范围。
def test_capability_semantically_empty_scope_omitted():
    from agent_py_agent.agent.capability_package_manifest import CapabilityDeclaration
    old = {'description': '能力', 'keywords': ['领域'], 'entry_document': 'CAPABILITY.md'}
    declaration = CapabilityDeclaration.from_payload({**old, 'application_scope': {
        'workbenches': [], 'domains': [], 'workspace_tags': []}})
    assert declaration.to_payload() == old


# LLM: 通知存储拒绝也是fail-open，不应把次级写入故障传播到主业务。
# 函数用途: 验证nofollow值错误不会中断源检查或断点返回。
def test_notification_store_value_error_is_fail_open(tmp_path, monkeypatch):
    # LLM: 只替换通知写口，模拟nofollow安全拒绝。
    # 函数用途: 产生一个明确的本地写入失败。
    def fail(*_args, **_kwargs):
        raise ValueError('unsafe notification path')
    monkeypatch.setattr(runtime, 'append_text_beneath', fail)
    result = runtime.emit_asset_im_notification(tmp_path, event_type='source_missing', title='失败', message='缺源', payload={})
    assert result['recorded'] is False and result['record_error'] == 'NOTIFICATION_STORE_UNAVAILABLE'


# LLM: 工具必须选择项目定义，执行项目定义不能把摘要写入被遮蔽的同名Owner资产。
# 函数用途: 覆盖工作流项目list/get/run及只读摘要边界。
def test_workspace_workflows_tools_do_not_update_owner_shadow(tmp_path):
    home, project = tmp_path / 'owner', tmp_path / 'project'
    project.mkdir()
    owner_definition = WorkflowDefinition(AssetDescriptor('scoped', 'workflow', 'Owner流程'), (
        WorkflowStepSpec('owner_step', 'Owner步骤', 'llm_skill_analysis'),))
    project_definition = WorkflowDefinition(AssetDescriptor('scoped', 'workflow', '项目流程'), (
        WorkflowStepSpec('project_step', '项目步骤', 'llm_skill_analysis'),))
    WorkflowStore(home).save_workflow(owner_definition)
    WorkflowStore(home, workspace_root=project).save_workflow(project_definition, target_scope='workspace')
    tool = WorkflowsTool(home, workspace_root_provider=lambda: project, analysis_handler=lambda *_: {'value': 2})
    listing = json.loads(tool.execute({'action': 'list'}).output)['workflows']
    assert listing[0]['descriptor']['name'] == '项目流程'
    got = json.loads(tool.execute({'action': 'get', 'workflow_id': 'scoped'}).output)
    assert got['descriptor']['scope_layer'] == 'workspace'
    result = tool.execute({'action': 'run', 'workflow_id': 'scoped'})
    assert result.ok and json.loads(result.output)['steps'][0]['step_id'] == 'project_step'
    assert WorkflowStore(home).get_workflow('scoped').descriptor.runtime_summary.run_count == 0


# LLM: 空称呼行不得跨行吞入下一条角色/背景描述，且同行#标题不得作为有效显示名，同名不同路径的包内Skill不得被set合并漏项。
# 函数用途: 覆盖USER.md称呼单行/标题边界解析与能力包同名不同路径资源统计。
def test_identity_local_name_does_not_cross_newline_when_empty(tmp_path):
    from agent_py_agent.agent.adapter.feishu_profile import resolve_user_space_identity
    from agent_py_agent.agent.capability.package_snapshot import package_assets
    from agent_py_agent.agent.capability_package_manifest import CapabilityFile
    home = tmp_path / 'owners' / 'local' / 'alice'
    home.mkdir(parents=True)
    (home / 'USER.md').write_text('- 称呼:\n- 角色/背景: 开发者\n', encoding='utf-8')
    res = resolve_user_space_identity(home)
    assert res['resolved'] is False and res['display_name'] is None
    (home / 'USER.md').write_text('- 称呼: # 敏感背景\n', encoding='utf-8')
    res_heading = resolve_user_space_identity(home)
    assert res_heading['resolved'] is False and res_heading['display_name'] is None
    assets = package_assets((
        CapabilityFile('methods/a/SKILL.md', '0' * 64),
        CapabilityFile('extras/a/SKILL.md', '1' * 64),
    ))
    assert assets['skills'] == ['extras/a', 'methods/a'] and len(assets['skills']) == 2
    dot_assets = package_assets((
        CapabilityFile('.a/SKILL.md', '0' * 64),
        CapabilityFile('a/SKILL.md', '1' * 64),
    ))
    assert dot_assets['skills'] == ['.a', 'a'] and len(dot_assets['skills']) == 2
    root_assets = package_assets((
        CapabilityFile('SKILL.md', '0' * 64),
        CapabilityFile('SKILL/SKILL.md', '1' * 64),
    ))
    assert root_assets['skills'] == ['SKILL', 'SKILL.md'] and len(root_assets['skills']) == 2


# LLM: v2实例调用user_space.identity.resolve不应因缺失identity_credentials抛异常，且无read能力时应拒绝403。
# 函数用途: 覆盖from_host_v2的身份查询与read能力门校验。
def test_host_v2_broker_identity_and_read_capability_gate(tmp_path):
    from agent_py_agent.agent import plugin_host_api
    from agent_py_agent.agent.gateway_parts.plugin_broker_api import PluginBrokerService
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant
    home, _, entry, ref = active_grant(tmp_path, sorted(host_capabilities()), scope='private')
    (home.home_dir / 'USER.md').write_text('- 称呼: 测试员\n', encoding='utf-8')
    plugin_host_api.set_host_api_base('http://127.0.0.1:18429')
    try:
        token = plugin_host_api.issue_host_api_env(ref, entry.manifest.plugin_id)[plugin_host_api.HOST_API_TOKEN_ENV]
        ports = HostDomainPorts()
        issuer = IngressIssuer()
        service = PluginBrokerService.from_host_v2(issuer.ingress(token), ports)
        resolved = service.dispatch('user_space.identity.resolve', {})
        assert resolved['ok'] is True and resolved['http_status'] == 200
        assert resolved['data']['display_name'] == '测试员'
        no_read_issuer = IngressIssuer(capability_policy=lambda: frozenset())
        denied_service = PluginBrokerService.from_host_v2(no_read_issuer.ingress(token), ports)
        for action in ('capability_packages.list', 'user_space.identity.resolve'):
            denied = denied_service.dispatch(action, {})
            assert denied['ok'] is False and denied['error_code'] == 'CAPABILITY_DENIED' and denied['http_status'] == 403
    finally:
        plugin_host_api.set_host_api_base(None)
