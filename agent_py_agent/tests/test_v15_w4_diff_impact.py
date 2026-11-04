"""差异和影响只消费结构化定义，未变化步骤必须折叠。"""
import pytest

from agent_py_agent.agent.assets.monitor_adapter import (
    get_monitor_descriptor,
    save_monitor_binding,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.tests.test_v15_w4_tool_surface import call, script_params, workflow_params


# LLM: code_diff按实际输出行计预算，不能用截断后的统计伪装完整差异。
# 函数用途: 验证脚本字段差异、统一diff容量及单行摘要。
def test_script_diff_and_summary_are_bounded(tmp_path):
    tool = ScriptsTool(tmp_path)
    call(tool, script_params(code='\n'.join('print(1)' for n in range(600))))
    call(tool, {'action': 'update', 'script_id': 'demo', 'code': '\n'.join('print(2)' for n in range(600)),
                'descriptor': {**script_params()['descriptor'], 'io_contract': {'outputs': ['value']}}})
    data = call(tool, {'action': 'diff', 'script_id': 'demo', 'from_version': 1})
    assert 'io_contract' in data['descriptor_changes']
    assert len(data['code_diff'].splitlines()) <= 400 and data['truncated'] is True
    assert len(data['summary_line']) <= 120 and '\n' not in data['summary_line']
    assert data == call(tool, {'action': 'diff', 'script_id': 'demo', 'from_version': 1, 'to_version': 2})


# LLM: 步骤按step_id而非列表下标对齐，未变内容不能再展开。
# 函数用途: 验证新增/变更/未变步骤的结构化差异。
def test_workflow_step_diff_folds_unchanged_steps(tmp_path):
    call(ScriptsTool(tmp_path), script_params())
    call(ScriptsTool(tmp_path), {**script_params(), 'script_id': 'other'})
    tool = WorkflowsTool(tmp_path)
    first = workflow_params()
    first['definition']['steps'].append({'step_id': 'stay', 'name': 'stay', 'mode': 'deterministic_script'})
    call(tool, first)
    changed = workflow_params()
    changed['definition']['steps'][0]['script_id'] = 'other'
    changed['definition']['steps'].extend([first['definition']['steps'][1],
        {'step_id': 'added', 'name': 'added', 'mode': 'llm_skill_analysis'}])
    call(tool, changed)
    data = call(tool, {'action': 'diff', 'workflow_id': 'flow', 'from_version': 1})
    assert [step['step_id'] for step in data['added']] == ['added']
    assert list(data['changed']) == ['fetch'] and 'script_id' in data['changed']['fetch']
    assert data['unchanged_count'] == 1 and data['folded'] == '其余 1 个步骤无变化'
    assert data['removed'] == [] and '步骤 +1 ~1 -0' in data['summary_line']


# LLM: impact比较当前对目标版本的输入输出字段，不执行或升级任何监控。
# 函数用途: 验证workflow影响报告中的IO字段及依赖投影。
def test_workflow_impact_includes_contract_changes(tmp_path):
    call(ScriptsTool(tmp_path), script_params())
    tool = WorkflowsTool(tmp_path)
    call(tool, workflow_params())
    changed = workflow_params()
    changed['definition']['descriptor']['io_contract'] = {'outputs': ['new']}
    call(tool, changed)
    data = call(tool, {'action': 'impact', 'workflow_id': 'flow', 'to_version': 1})
    assert data['io_contract_changes']['outputs'] == {'before': ['new'], 'after': []}
    assert data['dependents'] == [] and data['monitors'] == [] and data['compositions'] == []


@pytest.mark.parametrize('binding_kind', ['workflow', 'script', 'empty', 'target_only', 'composition'])
def test_monitor_producer_roundtrips_through_workflow_get_and_impact(tmp_path, binding_kind):
    """真实绑定的公开refs始终为列表，不能破坏同Owner任一工作流读口。"""
    scripts, tool = ScriptsTool(tmp_path), WorkflowsTool(tmp_path)
    call(scripts, script_params())
    call(scripts, {**script_params(), 'script_id': 'flow'})
    call(tool, workflow_params())
    call(tool, {'action': 'save', 'workflow_id': 'unrelated', 'definition': {
        'descriptor': {**workflow_params()['definition']['descriptor'], 'name': '无关工作流'},
        'steps': [{'step_id': 'judge', 'name': '分析', 'mode': 'llm_skill_analysis'}]}})
    bindings = {'alias': 'FLOW-WATCH', 'upgrade_policy': 'follow_compatible'}
    if binding_kind in ('workflow', 'composition'):
        bindings['workflow_ids'] = ['flow']
    elif binding_kind == 'script':
        bindings['script_ids'] = ['flow']
    elif binding_kind == 'target_only':
        bindings['execution_target'] = {'kind': 'workflow', 'asset_id': 'flow'}
    if binding_kind == 'composition':
        bindings['appended_steps'] = [{'step_id': 'tail', 'name': '追加分析', 'mode': 'llm_skill_analysis'}]
    produced = save_monitor_binding(tmp_path, 'watch', **bindings)
    wire = get_monitor_descriptor(tmp_path, 'watch')
    assert wire['refs'] == produced['refs'] and isinstance(wire['refs'], list)
    expected = ([{'monitor_id': 'watch', 'alias': 'flow-watch', 'pinned_version': 1,
                  'upgrade_policy': 'follow_compatible'}]
                if binding_kind in ('workflow', 'target_only', 'composition') else [])
    for workflow_id, monitors in [('flow', expected), ('unrelated', [])]:
        got = call(tool, {'action': 'get', 'workflow_id': workflow_id})
        impact = call(tool, {'action': 'impact', 'workflow_id': workflow_id, 'to_version': 1})
        assert got['monitors'] == impact['monitors'] == monitors
        if workflow_id == 'flow':
            assert [item['id'] for item in got['bound_methods']] == ['demo']
    if binding_kind == 'composition':
        hidden = call(tool, {'action': 'get', 'workflow_id': 'monitor-watch'})
        assert hidden['monitors'] == expected
        assert call(tool, {'action': 'impact', 'workflow_id': 'flow', 'to_version': 1})['compositions'] == [
            {'workflow_id': 'monitor-watch', 'appended_step_ids': ['tail']}]
    assert get_monitor_descriptor(tmp_path, 'watch') == wire
