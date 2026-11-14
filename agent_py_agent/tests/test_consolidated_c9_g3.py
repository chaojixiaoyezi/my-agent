"""Wave 3 consolidated tests (shard c9, group 3: test_v15_w3_turn_recommendations.py, test_v15_w4_diff_impact.py, test_v15_w4_path_guard.py, test_v15_w4_tool_guards.py, test_v15_w4_version_hook.py, test_v15_w5_analysis_gate.py, test_v15_w5_pinned_execution.py)."""
import inspect
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets import _AssetStore
from agent_py_agent.agent.assets.asset_metadata import HOST_MANAGED_METADATA_KEYS
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.monitor_adapter import get_monitor_descriptor, save_monitor_binding
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    latest_version,
    list_versions,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.capability import turn_recommendations as module
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.engine import WorkflowEngine, _script_pin
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.tests.test_v15_w4_tool_surface import call, script_params, workflow_params
from agent_py_agent.tests.test_v15_w5_foreach_batches import batch_flow
from agent_py_agent.tests.test_v15_w6_pinned_code_run import (
    CountingLocalRunner,
    save_transient_version,
)
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow

# ============================================================================
# Source: agent_py_agent/tests/test_v15_w3_turn_recommendations.py
# ============================================================================

# LLM: 只生成正式描述符字段，不向产品注入测试专用状态。
# 函数用途: 构造有IO合同和可选绑定的资产。
def asset(kind, identifier, *, refs=None, metadata=None, name=None, revision=1):
    return AssetDescriptor(identifier, kind, name or identifier, revision=revision,
        io_contract={'inputs': ['indicator', 'scope'], 'outputs': ['result', 'evidence_refs']},
        refs=refs or {}, metadata=metadata or {}).to_dict()


# LLM: 输出逐行JSON保持完整，测试不依赖自然语言推断机器决定。
# 函数用途: 解析推荐项的结构化字段。
def cards(text):
    return [json.loads(line[2:]) for line in text.splitlines() if line.startswith('- {')]


# LLM: 所有侧文件只写pytest私有根，不访问真实Owner。
# 函数用途: 准备一个合法JSON侧文件。
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


# LLM: 监控经标准refs引用父工作流，子脚本不能因折叠而丢失推荐资格。
# 函数用途: 建立I6的真实描述符和两条已知坑。
def i6_case(tmp_path):
    rows = [asset('workflow', 'W', refs={'scripts': ['S']}), asset('script', 'S')]
    monitors = [asset('monitor', 'monitor48', name='监控', refs={'workflows': ['W']}, metadata={'alias': 'job48'})]
    write_json(tmp_path / 'data/asset_evolution/pitfalls/script/S.json', {
        'schema_version': 'asset_pitfalls.v1', 'kind': 'script', 'asset_id': 'S',
        'entries': [{'text': '先验证scope边界'}, {'text': '保留原始证据引用'}],
    })
    return rows, monitors


# LLM: 直接验证规范字段与整段确定性，不把函数存在当成通过。
# 函数用途: 覆盖I6、EC1和同输入同字节。
def test_i6_lists_parent_child_contract_alias_and_pitfalls(tmp_path):
    rows, monitors = i6_case(tmp_path)
    text = module.render_turn_recommendations('job48 告警处理', rows, owner_home=tmp_path, monitors=monitors)
    assert text.startswith('## 本轮推荐的固定方法\n')
    by_id = {row['id']: row for row in cards(text)}
    assert set(by_id) == {'W', 'S'}
    assert by_id['S']['inputs'] == ['indicator', 'scope']
    assert by_id['S']['outputs'] == ['result', 'evidence_refs']
    assert by_id['S']['known_pitfalls'] == ['保留原始证据引用', '先验证scope边界']
    assert by_id['W']['bound_methods'] == ['script:S']
    assert '⏰ job48' in text and '优先调用' in text
    assert all('monitor_alias:job48' in row['matched_by'] for row in by_id.values())
    assert text == module.render_turn_recommendations('job48 告警处理', rows, owner_home=tmp_path, monitors=monitors)


# LLM: 每一评分来源都有独立资产，计分只影响软推荐而不授予动作。
# 函数用途: 验证权重、稳定并列顺序和top5下界。
def test_scoring_and_total_tie_order():
    rows = [asset('script', 'z', metadata={'trigger_matchers': {'intent_keywords': ['排查']}}),
            asset('workflow', 'w', metadata={'trigger_matchers': {'indicator_types': ['ipv4']}}),
            asset('script', 'delivery', name='信号查询', metadata={'trigger_matchers': {'alert_sources': ['sensor-a']}}),
            asset('script', 'a', metadata={'trigger_matchers': {'intent_keywords': ['排查']}})]
    index = module.build_recommendation_index(rows)
    ranked = index.match('{"source":"sensor-a","text":"排查 8.8.8.8"}')
    assert [row['id'] for row in ranked] == ['delivery', 'w', 'a', 'z']
    assert [row['score'] for row in ranked] == [40, 30, 20, 20]
    assert len(cards(module.render_turn_recommendations('排查', rows))) == 2


# LLM: 大目录不得溢出预算或输出截断JSON；最低排名可按合同被裁掉。
# 函数用途: 验证50个命中项的硬预算。
def test_cap_holds_with_fifty_matching_assets(tmp_path):
    rows = [asset('script', f'asset-{number:02d}', name='方法' * 50,
                  metadata={'trigger_matchers': {'intent_keywords': ['排查']}}) for number in range(50)]
    text = module.render_turn_recommendations('排查', rows, owner_home=tmp_path)
    assert 3 <= len(cards(text)) <= 5
    assert len(text.encode('utf-8')) <= 4 * module.TURN_RECOMMENDATION_MAX_TOKENS
    assert cards(text)[0]['id'] == 'asset-00'


# LLM: 隐藏composition永不推荐，但其监控别名应标注到可见base和子项。
# 函数用途: 验证隐藏组合重定向。
def test_hidden_composition_alias_is_redirected_to_visible_base():
    rows = [asset('workflow', 'base', refs={'scripts': ['child']}), asset('script', 'child'),
            asset('workflow', 'hidden', metadata={'hidden': True, 'composition': {'base_workflow_id': 'base'}})]
    monitors = [asset('monitor', 'm', refs={'workflows': ['hidden']}, metadata={'alias': 'job48'})]
    found = cards(module.render_turn_recommendations('job48', rows, monitors=monitors))
    assert {row['id'] for row in found} == {'base', 'child'}
    assert all(row['monitor_aliases'] == ['job48'] for row in found)


# LLM: 同一owner内重名监控不能静默挑一个目标；不同owner输入不会共用绑定。
# 函数用途: 验证别名歧义完整列候选。
def test_ambiguous_alias_lists_both_targets():
    rows = [asset('workflow', 'one'), asset('workflow', 'two')]
    monitors = [asset('monitor', f'm-{name}', refs={'workflows': [name]}, metadata={'alias': 'job48'}) for name in ('one', 'two')]
    assert {row['id'] for row in cards(module.render_turn_recommendations('JOB48', rows, monitors=monitors))} == {'one', 'two'}
    assert module.render_turn_recommendations('job480', rows, monitors=monitors) == ''


# LLM: 相同库存缓存同一纯索引，revision变化才切换实例；不在匹配中读取文件。
# 函数用途: 验证库存摘要缓存复用与失效。
def test_index_reuse_and_revision_invalidation():
    rows = [asset('script', 'one'), asset('workflow', 'two')]
    first = module.build_recommendation_index(rows)
    assert first is module.build_recommendation_index(list(reversed(rows)))
    changed = module.build_recommendation_index([{**rows[0], 'revision': 2}, rows[1]])
    assert changed is not first and changed.digest != first.digest


# LLM: 不良可选字段按缺失处理，不应击穿整个推荐或读取其他owner。
# 函数用途: 验证容错、便签上限、在用上限与路径隔离。
def test_malformed_fields_in_use_and_owner_scoped_notes(tmp_path):
    rows = [asset('script', 'one', metadata={'trigger_matchers': ['bad']})]
    write_json(tmp_path / 'data/asset_evolution/pitfalls/script/one.json', {'entries': 'bad'})
    notes = [{'file_name': f'doc-{number}.pdf', 'relative_path': f'media/input/{number}', 'text_excerpt': 'API说明'} for number in range(8)]
    notes.append({'file_name': 'FOREIGN', 'relative_path': '../foreign/private.pdf'})
    write_json(tmp_path / 'media/recent_inbound_notes.json', {'schema_version': 'inbound_notes.v1', 'entries': notes})
    text = module.render_turn_recommendations('one', rows, owner_home=tmp_path, in_use_method_ids=tuple(f'owner:method-{i}' for i in range(12)))
    assert '在用:' in text and 'owner:method-7' in text and 'owner:method-8' not in text
    assert '最近收到的文件:' in text and 'doc-7.pdf' in text and 'doc-2.pdf' not in text and 'FOREIGN' not in text
    (tmp_path / 'media/recent_inbound_notes.json').write_text('{broken')
    assert '最近收到的文件:' not in module.render_turn_recommendations('one', rows, owner_home=tmp_path)
    other = tmp_path / 'other-owner'
    write_json(other / 'media/recent_inbound_notes.json', {'entries': [{'file_name': 'PRIVATE-OTHER', 'relative_path': 'media/input/a'}]})
    (tmp_path / 'media/recent_inbound_notes.json').unlink()
    (tmp_path / 'media/recent_inbound_notes.json').symlink_to(other / 'media/recent_inbound_notes.json')
    assert 'PRIVATE-OTHER' not in module.render_turn_recommendations('one', rows, owner_home=tmp_path)


# LLM: 缺资产、无命中和无辅助信息保持原字节；纯Skill不制造固定资产段。
# 函数用途: 验证空段以及只有在用或便签时的合法尾段。
def test_empty_and_optional_only_sections(tmp_path):
    assert module.render_turn_recommendations('hello', []) == ''
    assert module.render_turn_recommendations('one', [asset('skill', 'one')]) == ''
    assert module.render_turn_recommendations('unrelated', [asset('script', 'one')]) == ''
    assert '在用:' in module.render_turn_recommendations('', [], in_use_method_ids=('owner:one',))
    write_json(tmp_path / 'media/recent_inbound_notes.json', {'entries': [{'file_name': 'doc.pdf', 'relative_path': 'media/input/doc'}]})
    assert '最近收到的文件:' in module.render_turn_recommendations('', [], owner_home=tmp_path)


# LLM: 调用真实接缝核对稳定目录与动态尾段隔离，不改builder或Router。
# 函数用途: 验证推荐实际接入本轮运行入口。
def test_resolve_tool_sections_wires_tier2_only_to_recommendations(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
    )
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.tests.test_tool_default_deferral import _agent

    agent = _agent(tmp_path)
    ScriptStore(agent.home_paths.owner_home_dir).save_script(
        AssetDescriptor('match-ip', 'script', '地址查询', metadata={'trigger_matchers': {'indicator_types': ['ipv4']}}), 'print("unused")')
    snapshot = agent.tools.runtime_snapshot()
    request = ToolSectionsRequest(agent, '8.8.8.8', None, snapshot, SimpleNamespace(source_protocol='native'))
    catalog, recommendations = _resolve_tool_sections(request)
    assert '## 本轮推荐的固定方法' in recommendations and 'match-ip' in recommendations
    assert '## 本轮推荐的固定方法' not in catalog
    assert catalog == _resolve_tool_sections(replace(request, user_prompt='hello'))[0]
    assert '## 本轮推荐的固定方法' not in _resolve_tool_sections(replace(request, context_scope='task_local'))[1]


# LLM: 走真实run到provider发包，附件字节仅pytest样本；网络和模型回复由既有本地socket替身提供。
# 函数用途: 否定只在纯提取器支持图片而主回合漏传媒体元数据的断线。
def test_real_run_passes_image_metadata_into_fixed_method_matcher(tmp_path, monkeypatch):
    import base64

    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.conversation.input_media import import_input_media_bytes
    from agent_py_agent.tests.test_compactcall_websocket import _Probe, _run_params, _setup

    agent, thread, prior, records = _setup(tmp_path, monkeypatch, False)
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('inspect-image', 'script', '图像关联',
        metadata={'trigger_matchers': {'indicator_types': ['image_attachment']}}), 'print("never executed")')
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0f8AAAAASUVORK5CYII=')
    media = import_input_media_bytes(png, agent.home_paths.owner_home_dir / 'media/input')
    params = _run_params(agent, thread, prior, records)
    params = replace(params, task_attributes={**params.task_attributes, 'input_media': [media]})
    probe = _Probe(monkeypatch, False, live=False)
    assert agent.run('处理这张图', params=params).response == '完成'
    wire = json.dumps(probe.calls[0]['body']['input'], ensure_ascii=False)
    assert '## 本轮推荐的固定方法' in wire and 'inspect-image' in wire


def test_typed_inventory_reuses_validation_but_rechecks_mutable_secret_fields(monkeypatch):
    descriptor = AssetDescriptor('typed', 'script', 'typed', metadata={
        'trigger_matchers': {'intent_keywords': ['排查']}})
    expected = module.render_turn_recommendations('排查', [descriptor.to_dict()])
    # frozen 描述符仍含可变容器；不能以类型或缓存替代每次安全投影。
    descriptor.metadata['api_key'] = 'private-fixture-only'
    descriptor.metadata['trigger_matchers']['intent_keywords'].append('Bearer private-fixture-only')
    expected_changed = module.render_turn_recommendations('排查', [descriptor.to_dict()])
    def unexpected(*args, **kwargs):
        pytest.fail('typed descriptor was serialized and reconstructed')
    monkeypatch.setattr(AssetDescriptor, 'from_dict', unexpected)
    actual = module.render_turn_recommendations('排查', [descriptor])
    assert actual == expected_changed == expected
    assert 'private-fixture-only' not in actual


def test_inventory_preserves_same_call_skill_values_only_when_requested(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import monitor_adapter, workspace_assets
    monkeypatch.setattr(workspace_assets.WorkspaceAssetOverlay, 'list_assets', lambda *args: [asset('script', 'entry')])
    monkeypatch.setattr(monitor_adapter, 'list_monitor_descriptors', lambda *args: [])
    entry = SimpleNamespace(stable_id='owner:skill:test', name='测试技能', description='公开名卡', source='owner')
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), tools=SimpleNamespace(workspace_root=None),
                            current_skill_snapshot=lambda: SimpleNamespace(enabled_entries=lambda: [entry]))
    runtime = SimpleNamespace(availability=SimpleNamespace(available=True), exposure=SimpleNamespace(model_visible=True))
    snapshot = SimpleNamespace(runtime=lambda name: runtime)
    ordinary, monitors, _ = module.inventory_for_agent(agent, runtime_snapshot=snapshot)
    typed, typed_monitors, _ = module.inventory_for_agent(agent, runtime_snapshot=snapshot, preserve_descriptors=True)
    assert all(isinstance(row, dict) for row in ordinary)
    assert isinstance(typed[-1], AssetDescriptor)
    assert [row.to_dict() if isinstance(row, AssetDescriptor) else row for row in typed] == ordinary
    assert module.render_turn_recommendations('entry', ordinary, monitors=monitors) == module.render_turn_recommendations('entry', typed, monitors=typed_monitors)


def test_descriptor_subclass_projection_keeps_original_validation_and_whitelist():
    class ExternalDescriptor(AssetDescriptor):
        def to_dict(self):
            return {**super().to_dict(), 'id': '../invalid', 'external_extra': 'ignored'}
    assert module._inventory_rows([ExternalDescriptor('safe', 'script', 'safe')]) == []
    class ExtraDescriptor(AssetDescriptor):
        def to_dict(self):
            return {**super().to_dict(), 'external_extra': 'ignored'}
    expected = module._inventory_rows([AssetDescriptor('safe', 'script', 'safe').to_dict()])
    assert module._inventory_rows([ExtraDescriptor('safe', 'script', 'safe')]) == expected


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w4_diff_impact.py
# ============================================================================

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


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w4_path_guard.py
# ============================================================================

# LLM: 两种模式不得绕过同一个canonical owner存储保护。
# 函数用途: 验证受管资产目录读写有别且workspace不受影响。
@pytest.mark.parametrize('collection,hint', [('scripts', 'scripts(action=update)'), ('workflows', 'workflows(action=save)')])
def test_owner_assets_readable_but_not_file_writable(tmp_path, collection, hint):
    home = tmp_path / 'owners/local/main'
    home.mkdir(parents=True)
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    path = home / collection / 'demo/main.py'
    assert policy.check(path).allowed
    denied = policy.check_write(path)
    assert not denied.allowed and denied.code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    assert hint in denied.message
    assert policy.check_write(home / 'tasks/project/.agent' / collection / 'demo/main.py').allowed


# LLM: 新侧账本沿既有H2硬门，不另造路径错误码。
# 函数用途: 验证三个宿主侧账本拒绝文件工具访问。
@pytest.mark.parametrize('name', ['asset_evolution', 'source_manifests', 'asset_extractions'])
def test_side_ledgers_are_host_managed(tmp_path, name):
    home = tmp_path / 'owners/local/main'
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    assert not policy.check_write(home / 'data' / name / 'x.json').allowed


# LLM: Darwin实际路径别名测试，不将模拟casefold当跨平台端到端证明。
# 函数用途: 拒绝大小写和firmlink别名下的资产写入。
@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin路径别名')
def test_darwin_case_and_realpath_alias(tmp_path):
    home = tmp_path / 'owners/local/main'
    home.mkdir(parents=True)
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    assert not policy.host_write_decision(home / 'Scripts/demo/main.py').allowed
    alias = tmp_path / 'alias'
    alias.symlink_to(home, target_is_directory=True)
    assert not policy.host_write_decision(alias / 'scripts/demo/main.py').allowed


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w4_tool_guards.py
# ============================================================================

# LLM: G3从模型请求剥离，可信Store写者不受模型参数影响。
# 函数用途: 验证strip回执和source_system保留。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_host_metadata_is_stripped_from_model_saves(tmp_path, kind):
    metadata = dict.fromkeys(HOST_MANAGED_METADATA_KEYS, 'supplied')
    metadata['source_system'] = 'source-a'
    tool = ScriptsTool(tmp_path) if kind == 'script' else WorkflowsTool(tmp_path)
    data = call(tool, script_params(metadata=metadata) if kind == 'script' else workflow_params(metadata=metadata))
    assert data['stripped_metadata_keys'] == sorted(HOST_MANAGED_METADATA_KEYS)
    descriptor = data if kind == 'script' else data['descriptor']
    assert not (set(descriptor['metadata']) & set(HOST_MANAGED_METADATA_KEYS))
    assert descriptor['metadata']['source_system'] == 'source-a'


# LLM: 两次持同一revision更新必须依次成功/冲突，不能因为G1而跳过原Store的CAS。
# 函数用途: 验证共享脚本更新缺revision拒绝及实际冲突。
def test_dependent_script_requires_revision_and_preserves_cas(tmp_path):
    scripts, workflows = ScriptsTool(tmp_path), WorkflowsTool(tmp_path)
    call(scripts, script_params())
    call(workflows, workflow_params())
    result = scripts.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'})
    assert not result.ok
    data = json.loads(result.output)
    assert data['error_code'] == 'ASSET_REVISION_REQUIRED'
    assert data['current_revision'] == 1
    assert {'kind': 'workflow', 'id': 'flow'} in data['dependents']
    call(scripts, {'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n', 'expected_revision': 1})
    stale = scripts.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(3)\n', 'expected_revision': 1})
    assert not stale.ok and json.loads(stale.output)['error_code'] == 'ASSET_REVISION_CONFLICT'
    assert scripts._store().get_script('demo')['code'] == 'print(2)\n'


# LLM: 注册表只归W12；行为断言与注册断言分开，不能由前一个失败遮住CAS验证。
# 函数用途: 显式保留两个待集成错误码的注册门。
@pytest.mark.parametrize('code', ['ASSET_REVISION_REQUIRED', 'ASSET_DRAFT_NOT_FOUND'])
def test_declared_action_error_registered(code):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    assert ToolHandlerOutcome('scripts', False, '{}', error_code=code).error_code == code


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w4_version_hook.py
# ============================================================================

# LLM: 只提供临时资产值，不执行代码或发现真实Owner。
# 函数用途: 构造同一脚本的完整描述符。
def descriptor(asset_id='demo', *, metadata=None):
    return AssetDescriptor(asset_id, 'script', asset_id, metadata=metadata or {})


# LLM: 版本断言来自W1公开读取口，不将返回revision误当发布版本。
# 函数用途: 验证首存、空白保存及实质变更的两个计数。
def test_first_save_whitespace_and_substantive_version(tmp_path):
    store = ScriptStore(tmp_path)
    first = store.save_script(descriptor(), 'print(1)\n')
    version = latest_version(tmp_path, 'script', 'demo')
    assert version is not None, '统一提交路径未记录首个正式版本'
    assert version['version'] == 1 and version['storage_revision'] == first['revision']
    same = store.save_script(descriptor(), 'print(1)  \r\n\r\n')
    assert same['revision'] == first['revision'] + 1
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1
    store.save_script(descriptor(), 'print(2)\n')
    assert [row['version'] for row in list_versions(tmp_path, 'script', 'demo')] == [2, 1]
    assert blob_get(tmp_path, get_version(tmp_path, 'script', 'demo', 1)['code_sha256']) == b'print(1)\n'


# LLM: 测试原始旧资产不经新钩子；只在临时Owner写入合法canonical旧格式。
# 函数用途: 首次覆盖无历史资产时保留legacy v1及新v2。
def test_legacy_preimage_including_renamed_entrypoint(tmp_path):
    root = tmp_path / 'scripts/demo'
    root.mkdir(parents=True)
    payload = descriptor().to_dict() | {'entrypoint': 'old.py', 'interpreter': 'python3',
                                      'timeout_seconds': 120, 'revision': 7}
    (root / 'script.json').write_text(json.dumps(payload))
    (root / 'old.py').write_text('print("old")\n')
    saved = ScriptStore(tmp_path).save_script(descriptor(), 'print("new")\n', entrypoint='new.py', expected_revision=7)
    previous = get_version(tmp_path, 'script', 'demo', 1)
    assert previous is not None, '旧descriptor pre-image尚未冻结'
    assert previous['legacy'] is True and previous['storage_revision'] == 7
    assert blob_get(tmp_path, previous['code_sha256']) == b'print("old")\n'
    assert get_version(tmp_path, 'script', 'demo', 2)['storage_revision'] == saved['revision'] == 8


# LLM: workflow保存使用完整步骤；对脚本的固定来源只能来自已提交代码SHA。
# 函数用途: 验证workflow版本保存完整定义、正文及脚本pin。
def test_workflow_pins_script_and_keeps_side_file(tmp_path):
    ScriptStore(tmp_path).save_script(descriptor(), 'print(1)\n')
    definition = WorkflowDefinition(AssetDescriptor('flow', 'workflow', 'flow'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),), '# 正文\n')
    saved = WorkflowStore(tmp_path).save_workflow(definition)
    record = latest_version(tmp_path, 'workflow', 'flow')
    assert record is not None, '工作流统一提交路径缺少版本钩子'
    assert record['pins']['scripts']['demo']['code_sha256'] == latest_version(tmp_path, 'script', 'demo')['code_sha256']
    assert record['pins']['scripts']['demo']['version'] == 1
    assert record['execution_definition_ref'] == saved.execution_definition_ref
    assert json.loads(blob_get(tmp_path, record['payload_blob']))['steps'][0]['script_id'] == 'demo'
    assert blob_get(tmp_path, record['side_blobs']['WORKFLOW.md']) == '# 正文\n'.encode()


# LLM: workspace与全局管理视图不生成Owner历史；晋升复用原正式提交路径。
# 函数用途: 验证scope隔离和promote_to_owner的版本。
def test_workspace_unversioned_and_promotion_versioned(tmp_path):
    from agent_py_agent.agent.assets.workspace_assets import promote_to_owner

    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    workspace.mkdir()
    ScriptStore(home, workspace_root=workspace).save_script(descriptor(), 'pass\n', target_scope='workspace')
    assert not (workspace / '.agent/data/asset_evolution').exists()
    assert latest_version(home, 'script', 'demo') is None
    promote_to_owner('script', 'demo', workspace_root=workspace, owner_home=home)
    assert latest_version(home, 'script', 'demo') is not None, '晋升未写Owner版本'
    assert latest_version(home, 'script', 'demo')['version'] == 1


# LLM: 正式历史只能写独立data根；删除仍沿原store目录算法且保留不可变快照。
# 函数用途: 验证历史不污染资产目录及delete软标记。
def test_delete_retains_history_and_asset_directory_is_unchanged(tmp_path):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert {path.name for path in (tmp_path / 'scripts/demo').iterdir()} == {'main.py', 'script.json'}
    assert latest_version(tmp_path, 'script', 'demo') is not None, '缺少正式历史'
    assert store.delete_script('demo') is True
    index = json.loads((tmp_path / 'data/asset_evolution/versions/script/demo.json').read_text())
    assert index['deleted_at']
    assert get_version(tmp_path, 'script', 'demo', 1) is not None


# LLM: 只向W1版本写入注入模拟I/O错误，不能使已提交Store保存回滚或冒充失败。
# 函数用途: 验证工具能见recorded=false且下次保存补录。
def test_hook_io_error_does_not_fail_commit_and_next_save_retries(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import revision_history

    original = revision_history.record_version

    # LLM: 错误发生在权威资产提交之后，参数签名与W1相同必要字段兼容。
    # 函数用途: 确定性模拟历史记录写入故障。
    def fail_record(owner_home, kind, asset_id, *, payload, code=None, side_files=None,
                    execution_definition_ref=None, storage_revision=None, pins=None,
                    summary_line='', published_at=None, legacy=False, restored_from=None, expected_latest=None):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(revision_history, 'record_version', fail_record)
    tool = ScriptsTool(tmp_path)
    result = tool.execute({'action': 'register', 'script_id': 'demo', 'descriptor': {
        'name': 'demo', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'code': 'pass\n'})
    assert result.ok, result.output
    receipt = json.loads(result.output)
    assert receipt.get('version_recording', {}).get('recorded') is False, '钩子失败未通过上下文暴露给工具'
    assert ScriptStore(tmp_path).get_script('demo')['code'] == 'pass\n'
    monkeypatch.setattr(revision_history, 'record_version', original)
    assert tool.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'}).ok
    assert latest_version(tmp_path, 'script', 'demo')['version'] >= 1


# LLM: 旧集合仍走原_write，但只有明确Owner的两类资产被版本化。
# 函数用途: 全局admin保存不在其根生成Owner历史。
def test_global_admin_save_is_not_owner_history(tmp_path):
    store = ScriptStore.for_global_admin(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert not (tmp_path / 'data/asset_evolution').exists()


# LLM: 一般运行摘要不是定义变更；不允许在私有spool元数据变化时产生新正式版本。
# 函数用途: 验证摘要fold与入口未变保存保持发布版本。
def test_run_summary_does_not_create_version(tmp_path):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'pass\n')
    assert latest_version(tmp_path, 'script', 'demo') is not None
    store.record_run_summary('demo', 'succeeded', 1)
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1


# LLM: 原_write异常必须保持资产与版本不变，不能先写历史再失败提交。
# 函数用途: 反证提交前失败不会形成虚构正式版本。
def test_store_commit_failure_does_not_record_new_version(tmp_path, monkeypatch):
    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'old\n')
    assert latest_version(tmp_path, 'script', 'demo') is not None
    original = store._put

    # LLM: 只拒绝新正文，原undo仍能恢复。
    # 函数用途: 在canonical提交前制造可恢复失败。
    def fail_new(asset_id, filename, content):
        if filename == 'main.py' and content == 'new\n':
            raise OSError('simulated I/O error')
        return original(asset_id, filename, content)

    monkeypatch.setattr(store, '_put', fail_new)
    with pytest.raises(OSError):
        store.save_script(replace(descriptor(), summary='new'), 'new\n')
    assert store.get_script('demo')['code'] == 'old\n'
    assert latest_version(tmp_path, 'script', 'demo')['version'] == 1


# LLM: 测试必须覆盖两种原始batch目标，Owner提交历史不能在整个batch回滚之前发表。
# 函数用途: 验证batch提交与workspace批次不会漏录或错误升版。
def test_owner_batch_records_versions_only_after_success(tmp_path):
    from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch

    commit_asset_batch([{'kind': 'script', 'id': 'demo', 'descriptor': descriptor().to_dict(),
                         'code': 'pass\n'}], owner_home=tmp_path)
    assert latest_version(tmp_path, 'script', 'demo') is not None, 'batch未接版本钩子'


# LLM: 明确测试单提交接线，不以工具wrapper代替公共store入口。
# 函数用途: 保留原基座签名以守住W7保存合同。
def test_original_store_signatures_remain():
    import inspect

    assert list(inspect.signature(_AssetStore._locked).parameters) == ['self']
    assert list(inspect.signature(ScriptStore.save_script).parameters)[:3] == ['self', 'descriptor', 'code']


# LLM: 指纹不变不允许新建版本，但workflow pin必须落到同一条版本记录的原始代码SHA。
# 函数用途: 反证空白保存后的版本与SHA不会错配。
def test_whitespace_script_pin_matches_published_record(tmp_path):
    scripts = ScriptStore(tmp_path)
    scripts.save_script(descriptor(), 'print(1)\n')
    scripts.save_script(descriptor(), 'print(1)  \r\n\r\n')
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', 'flow'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),)))
    pin = latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts']['demo']
    record = get_version(tmp_path, 'script', 'demo', pin['version'])
    assert pin['code_sha256'] == record['code_sha256']
    assert len(list_versions(tmp_path, 'script', 'demo')) == 1


# LLM: 版本钩子自身的异常也发生在canonical提交之后；不能只防W1内部的IO异常。
# 函数用途: 反证提交后钩子异常不会让保存或删除失败。
@pytest.mark.parametrize('operation', ['save', 'delete'])
def test_hook_callback_failure_never_changes_commit_result(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.assets import version_hook

    store = ScriptStore(tmp_path)
    store.save_script(descriptor(), 'old\n')

    # LLM: 故障来自钩子调用边界，不能进入原事务undo。
    # 函数用途: 模拟导入/回调初始化异常。
    def fail_hook(store, payload, previous=None, updates=None):
        raise ImportError('simulated hook initialization error')

    monkeypatch.setattr(version_hook, 'record_committed' if operation == 'save' else 'record_deleted', fail_hook)
    with version_hook.version_operation() as state:
        if operation == 'save':
            saved = store.save_script(descriptor(), 'new\n')
            assert saved['code'] == 'new\n'
        else:
            assert store.delete_script('demo') is True
            assert store.get_script('demo') is None
        assert state['recording']['recorded'] is False


# LLM: legacy快照的执行身份同样可供W8精确查找，不从新内容推导旧引用。
# 函数用途: 验证旧脚本pre-image保留原执行引用。
def test_legacy_preimage_has_execution_reference(tmp_path):
    root = tmp_path / 'scripts/demo'
    root.mkdir(parents=True)
    payload = descriptor().to_dict() | {'entrypoint': 'main.py', 'interpreter': 'python3',
                                      'timeout_seconds': 120, 'revision': 4}
    (root / 'script.json').write_text(json.dumps(payload))
    (root / 'main.py').write_text('old\n')
    store = ScriptStore(tmp_path)
    previous = store.get_script('demo')['execution_definition_ref']
    store.save_script(descriptor(), 'new\n')
    assert get_version(tmp_path, 'script', 'demo', 1)['execution_definition_ref'] == previous


def test_unchanged_legacy_workflow_save_records_complete_script_pins(tmp_path):
    from agent_py_agent.agent.assets.workflow_store import with_workflow_step_refs

    ScriptStore(tmp_path).save_script(descriptor(), 'print(1)\n')
    definition = with_workflow_step_refs(WorkflowDefinition(
        AssetDescriptor('flow', 'workflow', '旧工作流', revision=7),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),)))
    root = tmp_path / 'workflows/flow'
    root.mkdir(parents=True)
    payload = definition.descriptor.to_dict() | {'steps': [step.to_dict() for step in definition.steps],
                                               'body_markdown': ''}
    (root / 'workflow.json').write_text(json.dumps(payload))
    (root / 'WORKFLOW.md').write_text('')
    assert latest_version(tmp_path, 'workflow', 'flow') is None
    saved = WorkflowStore(tmp_path).save_workflow(definition, expected_revision=7)
    assert saved.descriptor.revision == 8
    legacy = get_version(tmp_path, 'workflow', 'flow', 1)
    assert legacy['legacy'] is True and legacy['pins']['scripts']['demo']['version'] == 1
    assert latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts'] == legacy['pins']['scripts']


def composition_fixture(home):
    scripts, workflows = ScriptStore(home), WorkflowStore(home)
    scripts.save_script(descriptor(), 'print(1)\n')
    base = workflows.save_workflow(WorkflowDefinition(AssetDescriptor('base', 'workflow', '基础流程'),
        (WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),), '# 历史正文'))
    published = latest_version(home, 'workflow', 'base')
    scripts.save_script(descriptor(), 'print(2)\n')
    hidden = replace(base, descriptor=AssetDescriptor('monitor-watch', 'workflow', '隐藏组合', metadata={
        'hidden': True, 'base_workflow_pin': {'id': 'base', 'version': published['version']},
        'composition': {'monitor_id': 'watch', 'base_workflow_id': 'base', 'base_version': published['version'],
                        'appended_step_ids': ['tail']}}))
    return scripts, workflows, hidden, published


def test_composition_hook_keeps_historical_base_pins_and_appended_dependencies(tmp_path):
    scripts, workflows, hidden, base = composition_fixture(tmp_path)
    scripts.save_script(descriptor('other'), 'print(3)\n')
    hidden = replace(hidden, steps=hidden.steps + (
        WorkflowStepSpec('tail', '追加查询', 'deterministic_script', script_id='other'),))
    workflows.save_workflow(hidden)
    published = latest_version(tmp_path, 'workflow', 'monitor-watch')
    assert published['pins']['scripts']['demo'] == base['pins']['scripts']['demo']
    assert published['pins']['scripts']['other']['version'] == 1
    assert published['pins']['workflows'] == {'base': {'version': base['version']}}
    assert blob_get(tmp_path, published['side_blobs']['WORKFLOW.md']) == '# 历史正文'.encode()
    workflows.save_workflow(hidden)
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') == published


def test_composition_hook_does_not_publish_conflicting_dependency_identities(tmp_path):
    from agent_py_agent.agent.assets.version_hook import version_operation

    _, workflows, hidden, _ = composition_fixture(tmp_path)
    hidden = replace(hidden, steps=hidden.steps + (
        WorkflowStepSpec('tail', '同名但不同版本查询', 'deterministic_script', script_id='demo'),))
    with version_operation() as operation:
        workflows.save_workflow(hidden)
    assert operation['recording']['recorded'] is False
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') is None


@pytest.mark.parametrize('missing', ['base_payload', 'script_code'])
def test_composition_hook_rejects_incomplete_base_snapshots(tmp_path, missing):
    from agent_py_agent.agent.assets.version_hook import version_operation

    _, workflows, hidden, base = composition_fixture(tmp_path)
    digest = (base['payload_blob'] if missing == 'base_payload'
              else get_version(tmp_path, 'script', 'demo', base['pins']['scripts']['demo']['version'])['code_sha256'])
    (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    with version_operation() as operation:
        workflows.save_workflow(hidden)
    assert operation['recording']['recorded'] is False
    assert latest_version(tmp_path, 'workflow', 'monitor-watch') is None


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w5_analysis_gate.py
# ============================================================================

# LLM: 仅替代W6运行seam；工作流创建、依赖解析及账本均为真实实现。
# 函数用途: 构造携带结构化query_health的脚本结果。
def result(health):
    return ScriptExecutionResult('collect', True, 0, 1, 1, 2, {'value': 1}, '', '',
                                 source_check={'query_health': health})


# LLM: 模型回调只记录是否真实分派，禁止以自然语言输出判门。
# 函数用途: 锁定阻断、健康、畸形结构及错误策略。
@pytest.mark.parametrize('health,blocked', [
    ({'analysis_blocked': True, 'block_reason': 'QUERY_FAILED_NO_DATA',
      'degraded_sources': [{'source_id': 'apps'}]}, True),
    ({'analysis_blocked': False, 'block_reason': None}, False),
    (None, False), ([], False), ({'analysis_blocked': 'true', 'block_reason': 'x'}, False),
    ({'analysis_blocked': True}, False)])
@pytest.mark.parametrize('mode', ['llm_skill_analysis', 'llm_prompt', 'hybrid'])
def test_query_health_gate_never_dispatches_blocked_analysis(tmp_path, monkeypatch, health, blocked, mode):
    script(tmp_path)

    # LLM: W6 seam替身保留显式参数合同，不屏蔽签名错误。
    # 函数用途: 返回本例健康信封。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        return result(health)

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources',
                        execute)
    seen = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: seen.append(True) or {'decision': 'ok'})
    judge = step('judge', mode=mode, script_id='collect' if mode == 'hybrid' else '', on_error='fail',
                 inputs_map={'value': {'step_id': 'collect', 'key': 'value'}})
    run = engine.run_workflow(workflow(step(), judge))
    if blocked:
        assert seen == [] and run.status == 'failed'
        assert run.steps[-1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
        assert run.steps[-1].output_snapshot['data_retrieval_failed'] is True
        assert run.steps[-1].output_snapshot['source_ids'] == ['apps']
    else:
        assert seen == [True] and run.status == 'succeeded'
    if isinstance(health, dict):
        assert run.steps[0].escalation_result['query_health'] == health


# LLM: 合同由传给分析器的上下文验证，不只检查配置是否存在。
# 函数用途: 验证默认、显式和非法隔离模式的替换回执。
@pytest.mark.parametrize('configured,expected,replaced', [
    (None, 'stateless_skill', False), ('stateless_skill', 'stateless_skill', False),
    ('lightweight_subagent', 'lightweight_subagent', False), ('invalid', 'stateless_skill', True)])
def test_analysis_isolation_is_passed_to_handler(tmp_path, configured, expected, replaced):
    seen = []

    # LLM: 模拟分析器只接收引擎给出的隔离请求，不读取当前会话。
    # 函数用途: 捕获实际派发的隔离参数。
    def analyze(spec, context, skills):
        seen.append(getattr(context, 'run_params', {}))
        return {'decision': 'ok'}

    config = {} if configured is None else {'analysis_isolation': configured}
    run = WorkflowEngine(tmp_path, analysis_handler=analyze).run_workflow(
        workflow(step('judge', mode='llm_skill_analysis', config=config)))
    assert run.status == 'succeeded'
    assert seen == [{'analysis_isolation': expected}]
    assert bool(run.steps[0].escalation_result.get('analysis_isolation_replaced')) is replaced


# LLM: 每批脚本结果有独立健康事实，坏批不得污染健康批的分派资格。
# 函数用途: 验证hybrid逐批门、阻断统计和继续策略。
def test_hybrid_foreach_gates_only_blocked_batch(tmp_path, monkeypatch):
    script(tmp_path)
    seen = []

    # LLM: W6 seam根据本批输入给出机器事实，不以模型输出作判断。
    # 函数用途: 让第二批的取数明确失败。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        blocked = inputs['batch'][0] == 2
        return replace(result({'analysis_blocked': blocked, 'block_reason': 'QUERY_FAILED_NO_DATA' if blocked else None}),
                       output_data={'values': inputs['batch']})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    value = batch_flow(policy='continue', mode='aggregate')
    value['steps'][0].update(mode='hybrid', script_id='collect')
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.step_inputs['batch'][0]) or {'values': context.step_inputs['batch']}).run_workflow(
            value, {'rows': list(range(6))})
    assert run.status == 'succeeded' and seen == [0, 4]
    assert run.steps[0].batches_summary == {'total': 3, 'completed': 2, 'failed': 0, 'blocked': 1, 'skipped': 0}
    assert run.steps[0].batches[1]['analysis_blocked'] is True
    assert run.final_output == {'values': [0, 1, 4, 5]}


# LLM: 无映射的旧分析合同确实携带全部前步，不能据空dependencies误称没有输入依赖。
# 函数用途: 证伪独立复核中关于隐式前步上下文的假设。
def test_unmapped_analysis_really_receives_prior_outputs(tmp_path, monkeypatch):
    script(tmp_path)
    seen = []

    # LLM: 仅替代seam，保留真实查询健康投影及旧分析上下文。
    # 函数用途: 提供一份健康的前步输出。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        return result({'analysis_blocked': False, 'block_reason': None})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.prior_outputs) or {'decision': 'ok'}).run_workflow(
            workflow(step(), step('judge', mode='llm_skill_analysis')))
    assert run.status == 'succeeded' and seen == [{'collect': {'value': 1}}]


# LLM: 不同源列表的批号没有健康关联权；父输入的明确阻断不能被同号健康批覆盖。
# 函数用途: 锁住独立复核发现的错误批号关联反例。
def test_unrelated_batch_index_cannot_override_blocked_input():
    from agent_py_agent.agent.workflows.analysis_gate import apply_analysis_gate
    from agent_py_agent.agent.workflows.models import StepExecutionRecord

    source = StepExecutionRecord('source', 'source', 'deterministic_script', 'succeeded', 0, 0, 0, False,
        escalation_result={'query_health': {'analysis_blocked': True, 'block_reason': 'QUERY_FAILED_NO_DATA'}},
        batches=[{'index': 0, 'query_health': {'analysis_blocked': False, 'block_reason': None}}])
    target = StepExecutionRecord('judge', 'judge', 'llm_skill_analysis', 'running', 0, 0, 0, False)
    spec = {'step_id': 'judge', 'on_error': 'fail', '_batch_index': 0,
            'inputs_map': {'value': {'step_id': 'source', 'key': 'value'}}}
    assert apply_analysis_gate(target, spec, (source,)) is True
    assert target.error_code == 'ANALYSIS_BLOCKED_NO_DATA'


# LLM: 父步骤也消费所有本批脚本健康，不能只在嵌套rows保留阻断导致下游误判。
# 函数用途: 验证continue模式的完整健康投影。
def test_parent_foreach_health_retains_any_blocked_batch(tmp_path, monkeypatch):
    script(tmp_path)

    # LLM: 第二批明确取数失败，第三批健康不应抹掉它。
    # 函数用途: 构造先后不同健康的真实批链。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        blocked = inputs['batch'][0] == 2
        return result({'analysis_blocked': blocked, 'block_reason': 'QUERY_FAILED_NO_DATA' if blocked else None})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    value = batch_flow(policy='continue', mode='aggregate')
    value['steps'][0].update(mode='hybrid', script_id='collect')
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'value': 1}).run_workflow(
        value, {'rows': list(range(6))})
    assert run.steps[0].escalation_result.get('query_health', {}).get('analysis_blocked') is True


# LLM: foreach的子分析上下文与父步骤都必须保留隔离选择的替换事实。
# 函数用途: 验证顺序及并发批的非法隔离值不丢失回执。
@pytest.mark.parametrize('parallel', [1, 2])
def test_foreach_notes_invalid_isolation_on_parent_record(tmp_path, parallel):
    seen = []
    value = batch_flow(parallel=parallel)
    value['steps'][0]['config'] = {'analysis_isolation': 'invalid'}
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.run_params['analysis_isolation']) or {'value': 1}).run_workflow(
            value, {'rows': [1, 2, 3]})
    assert run.status == 'succeeded' and seen == ['stateless_skill', 'stateless_skill']
    assert run.steps[0].escalation_result.get('analysis_isolation_replaced') is True


# ============================================================================
# Source: agent_py_agent/tests/test_v15_w5_pinned_execution.py
# ============================================================================

# LLM: 版本由W1原接口发表，不手写历史记录。
# 函数用途: 冻结脚本真实正文和执行身份。
def publish_script(home):
    row = ScriptStore(home).get_script('collect')
    pin = _script_pin('collect', row)
    version = record_version(home, 'script', 'collect', payload=row, code=row['code'].encode(),
                             execution_definition_ref=pin['execution_definition_ref'], storage_revision=row.get('revision', 0))
    return pin, version['record']


# LLM: 只替代尚未集成的W6历史代码执行；严格核真实引擎传入pin。
# 函数用途: 观察运行seam实际收到的冻结代码摘要。
def observe_seam(monkeypatch):
    seen = []

    # LLM: 结果是组件替身，不声称执行了历史脚本字节。
    # 函数用途: 回传本次seam收到的代码身份。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        seen.append(expected_script_pin)
        return ScriptExecutionResult(script_id, True, 0, 1, 1, 2,
                                     {'value': expected_script_pin['code_sha256']}, '', '')

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    return seen


# LLM: 必须在创建运行之前拒绝未知版本，不能留下幽灵run。
# 函数用途: 验证公开keyword和结构化版本缺失回执。
def test_unknown_workflow_version_has_no_run(tmp_path):
    parameter = inspect.signature(WorkflowEngine.run_workflow).parameters.get('definition_version')
    assert parameter is not None and parameter.kind == parameter.KEYWORD_ONLY and parameter.default is None
    WorkflowStore(tmp_path).save_workflow(workflow(step('judge', mode='llm_skill_analysis')))
    result = WorkflowEngine(tmp_path).run_workflow('daily', definition_version=99)
    assert result['error_code'] == 'ASSET_VERSION_NOT_FOUND' and result['effect_outcome'] == 'not_started'
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))


# LLM: 当前脚本及工作流均改变后，版本运行仍从历史定义、Markdown与脚本pins恢复。
# 函数用途: 验证workflow@v1连锁固定script@v1及持久化字段。
@pytest.mark.parametrize('flat', [False, True])
def test_workflow_version_uses_history_definition_and_chained_script_pin(tmp_path, monkeypatch, flat):
    assert 'definition_version' in inspect.signature(WorkflowEngine.run_workflow).parameters
    script(tmp_path)
    old_pin, old = publish_script(tmp_path)
    store = WorkflowStore(tmp_path)
    saved = store.save_workflow(workflow(step(on_error='fail')))
    payload = saved.to_dict()
    if flat:
        payload = {**saved.descriptor.to_dict(), 'steps': [item.to_dict() for item in saved.steps], 'body_markdown': ''}
    record_version(tmp_path, 'workflow', 'daily', payload=payload, side_files={'WORKFLOW.md': 'historical body'},
                   pins={'scripts': {'collect': {'version': old['version'], 'code_sha256': old_pin['code_sha256'],
                                                'execution_definition_ref': old_pin['execution_definition_ref']}}})
    script(tmp_path, 'print(\'{"value":2}\')')
    publish_script(tmp_path)
    store.save_workflow(workflow(step('current', mode='llm_skill_analysis')))
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: pytest.fail('current definition executed'))
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'succeeded' and run.definition_version == 1
    assert len(seen) == 1 and seen[0]['code_sha256'] == old_pin['code_sha256']
    assert run.final_output == {'value': old_pin['code_sha256']}
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['body_markdown'] == 'historical body'
    assert snapshot['_frozen_scripts']['collect']['code_sha256'] == old_pin['code_sha256']
    assert engine.store.load_run(run.workflow_run_id).definition_version == 1


# LLM: 在途运行冻结旧sha，显式allow才可改用当前；缺blob时保留原漂移门。
# 函数用途: 验证EC39恢复、缺失历史及显式重新钉住。
@pytest.mark.parametrize('remove_blob,allow,expected_status', [
    (False, False, 'succeeded'), (True, False, 'paused_for_resume'), (True, True, 'succeeded')])
def test_resume_preserves_historical_pin_unless_explicitly_allowed(tmp_path, monkeypatch, remove_blob, allow, expected_status):
    script(tmp_path)
    old_pin, _ = publish_script(tmp_path)
    seen = observe_seam(monkeypatch)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'_pause_for_resume': True})
    run = engine.run_workflow(workflow(step('pause', mode='llm_skill_analysis'), step(on_error='fail')))
    assert run.status == 'paused_for_resume' and not seen
    script(tmp_path, 'print(\'{"value":2}\')')
    current_pin, _ = publish_script(tmp_path)
    if remove_blob:
        sha = old_pin['code_sha256']
        (tmp_path / 'data/asset_evolution/blobs' / sha[:2] / sha).unlink()
    resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True},
                                          allow_script_drift=allow)
    assert resumed.status == expected_status
    if expected_status == 'succeeded':
        expected = current_pin if allow else old_pin
        assert seen[-1]['code_sha256'] == expected['code_sha256']
        assert resumed.final_output['value'] == expected['code_sha256']
    else:
        assert not seen and resumed.steps[1].error_code == 'SCRIPT_REVISION_DRIFT'


# LLM: 正式Store及发布钩子冻结v1，不直接拼写历史文件或执行结果。
# 函数用途: 创建同代码/ref、不同重试许可的工作流历史。
def retry_workflow_versions(home, *, old_policy=False, current_policy=True, pause=False, foreach=None):
    first = save_transient_version(home, idempotent=old_policy)
    specs = ([step('pause', mode='llm_skill_analysis')] if pause else [])
    specs.append(step(script_id='probe', on_error='fail', **({'foreach': foreach} if foreach else {})))
    WorkflowStore(home).save_workflow(workflow(*specs))
    published = get_version(home, 'workflow', 'daily', 1)
    assert published['pins']['scripts']['probe']['version'] == 1
    current = save_transient_version(home, idempotent=current_policy)
    assert first['code'] == current['code']
    assert first['execution_definition_ref'] == current['execution_definition_ref']
    assert first['revision'] != current['revision']
    return first, current


# LLM: 目录枚举本身的FD每次以with关闭；测量仅覆盖本进程，不外推平台沙箱或真实服务。
# 函数用途: 统计当前进程的真实打开FD，供成功及拒绝路径前后对照。
def fd_count():
    with os.scandir('/proc/self/fd' if Path('/proc/self/fd').exists() else '/dev/fd') as entries:
        return sum(1 for _ in entries)


# LLM: 独立打开/关闭控制证明观察器对一个实际FD有响应，防止把常量计数当作零泄漏。
# 函数用途: 验证FD观察点自身有效。
def test_fd_observation_detects_open_and_close():
    before = fd_count()
    with open(os.devnull) as stream:
        assert stream.fileno() >= 0 and fd_count() == before + 1
    assert fd_count() == before


# LLM: 真实编号历史必须一路保留到原runner；无异常的健康红灯不能成为额外执行许可。
# 函数用途: 验证正式、恢复及批执行的历史策略，和反向当前策略收窄。
@pytest.mark.parametrize('route,old_policy,current_policy', [
    ('normal', False, True), ('resume', False, True), ('foreach', False, True), ('normal', True, False)])
def test_versioned_workflow_keeps_equal_code_retry_provenance(tmp_path, route, old_policy, current_policy):
    batching = {'from': 'inputs.items', 'as': 'batch', 'batch_size': 1, 'max_parallel': 1,
                'mode': 'map'} if route == 'foreach' else None
    _, current = retry_workflow_versions(tmp_path, old_policy=old_policy, current_policy=current_policy,
        pause=route == 'resume', foreach=batching)
    script_json = tmp_path / 'scripts/probe/script.json'
    before_bytes = script_json.read_bytes()
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'_pause_for_resume': True})
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    run = engine.run_workflow('daily', {'items': [1, 2]} if batching else {}, definition_version=1)
    if route == 'resume':
        assert run.status == 'paused_for_resume' and not engine.runner.calls
        run = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True})
    assert run.status == 'succeeded' and run.definition_version == 1
    assert len(engine.runner.calls) == (2 if batching else 1)
    assert all(isinstance(call['history'], dict) and call['history'].get('version') == 1
               for call in engine.runner.calls)
    assert all(call['record']['metadata']['retry_policy']['idempotent'] is False for call in engine.runner.calls)
    assert all(value.ok and value.exit_code == 0 for value in engine.runner.results)
    assert all(value.source_check['pin_resolution']['version'] == 1 for value in engine.runner.results)
    health = run.steps[-1].escalation_result['query_health']
    assert health['classification'] == 'TRANSIENT_FAILURE_EMPTY' and health['analysis_blocked'] is True
    assert health['retries'] == []
    snapshot = engine.store.load_workflow_snapshot(run.workflow_run_id)
    assert snapshot['_frozen_scripts']['collect']['version'] == 1
    assert engine.store.load_run(run.workflow_run_id).definition_version == 1
    assert script_json.read_bytes() == before_bytes
    assert ScriptStore(tmp_path).get_script('probe')['revision'] == current['revision']
    assert get_version(tmp_path, 'script', 'probe', 2)['version'] == 2
    assert not list((tmp_path / 'tmp/script_exec').iterdir())
    assert all(not Path(value.output_data['stage']).exists() for value in engine.runner.results)
    assert fd_count() - before_fd == 0


# LLM: 定义读取后才移走历史blob，确保触达_script_step接缝，不把入口已拒绝算作调用修复。
# 函数用途: 相同当前代码存在时，历史code/payload缺失仍零执行且不改v2。
@pytest.mark.parametrize('missing', ['code_sha256', 'payload_blob'])
def test_equal_code_history_missing_after_definition_resolution_refuses_run(tmp_path, monkeypatch, missing):
    retry_workflow_versions(tmp_path)
    history = get_version(tmp_path, 'script', 'probe', 1)
    digest = history[missing]
    blob = tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest
    original = WorkflowEngine._script_step
    reached = []

    # LLM: 仅控制历史在入口和步骤之间消失的时机；真正步骤、runtime和runner均不替换结果。
    # 函数用途: 在已解析历史定义之后制造一次明确的缺失观察。
    def remove_history(engine, record, spec, run, definition, *, record_summary=True):
        reached.append(spec['step_id'])
        blob.unlink()
        return original(engine, record, spec, run, definition, record_summary=record_summary)

    monkeypatch.setattr(WorkflowEngine, '_script_step', remove_history)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    current_bytes = (tmp_path / 'scripts/probe/script.json').read_bytes()
    before_fd = fd_count()
    run = engine.run_workflow('daily', definition_version=1)
    assert reached == ['collect']
    assert run.status == 'failed' and run.steps[0].error_code == 'SCRIPT_REVISION_DRIFT'
    assert engine.runner.calls == []
    assert (tmp_path / 'scripts/probe/script.json').read_bytes() == current_bytes
    assert fd_count() - before_fd == 0


# LLM: 历史enabled并不授当前Owner执行权，停用在引擎原门拒绝，当前记录保持停用。
# 函数用途: 验证版本运行不能复活当前关闭的脚本。
def test_equal_code_current_disablement_blocks_historical_workflow(tmp_path):
    retry_workflow_versions(tmp_path)
    ScriptStore(tmp_path).set_enabled('probe', False)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'failed' and run.steps[0].error_code == 'ASSET_DISABLED_BY_OWNER'
    assert engine.runner.calls == []
    assert ScriptStore(tmp_path).get_script('probe')['enabled'] is False
    assert fd_count() - before_fd == 0


# LLM: debug别名共用_script_step，但无历史选择时仍执行当前；record_summary=False禁止正式写账。
# 函数用途: 验证两条debug脚本入口继续隔离运行、摘要及输出目标。
@pytest.mark.parametrize('mode', ['deterministic_script', 'script_transform'])
def test_current_script_debug_keeps_isolation_and_summary_false(tmp_path, mode):
    save_transient_version(tmp_path, idempotent=True)
    WorkflowStore(tmp_path).save_workflow(workflow(step(script_id='probe', mode=mode)))
    script_json = tmp_path / 'scripts/probe/script.json'
    before_bytes = script_json.read_bytes()
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    before_fd = fd_count()
    result = engine.debug_step('daily', 'collect', allow_side_effects=True)
    assert result['status'] == 'succeeded'
    assert len(engine.runner.calls) == 3
    assert all(call['history'] is None and call['record_summary'] is False for call in engine.runner.calls)
    assert script_json.read_bytes() == before_bytes
    assert ScriptStore(tmp_path).get_script('probe')['runtime_summary']['run_count'] == 0
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))
    assert not list((tmp_path / 'tmp/script_exec').iterdir())
    assert fd_count() - before_fd == 0


# LLM: Event固定旧步骤读取早于终态发表；晚到步骤及普通run写不得复活成功、失败或取消。
# 函数用途: 在真实版本工作流恢复/取消后验证终态单调。
@pytest.mark.parametrize('terminal', ['succeeded', 'failed', 'cancelled'])
def test_versioned_terminal_rejects_event_ordered_late_writes(tmp_path, terminal):
    retry_workflow_versions(tmp_path, pause=True)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'_pause_for_resume': True})
    engine.runner = CountingLocalRunner(tmp_path)
    run = engine.run_workflow('daily', definition_version=1)
    assert run.status == 'paused_for_resume'
    loaded, publish = threading.Event(), threading.Event()
    before_fd = fd_count()

    # LLM: 旧DTO来自真实Store，等待主线程完成终态后才交回原持久化入口。
    # 函数用途: 确定性复现晚到的旧步骤发表及普通状态倒退尝试。
    def late_writer():
        old = engine.store.load_run(run.workflow_run_id)
        loaded.set()
        assert publish.wait(15)
        with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
            engine.store.save_step_result(run.workflow_run_id, replace(old.steps[-1], status='running'))
        current = engine.store.load_run(run.workflow_run_id)
        with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL'):
            engine.store.save_run(replace(current, status='running'))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(late_writer)
        try:
            assert loaded.wait(15)
            if terminal == 'cancelled':
                assert engine.cancel_workflow_run(run.workflow_run_id,
                    expected_revision=run.revision, operation_key='cancel-pinned') == []
            else:
                if terminal == 'failed':
                    ScriptStore(tmp_path).set_enabled('probe', False)
                resumed = engine.resume_workflow_run(run.workflow_run_id, patched_step_output={'ready': True})
                assert resumed.status == terminal
        finally:
            publish.set()
        future.result(timeout=15)
    assert engine.store.load_run(run.workflow_run_id).status == terminal
    assert len(engine.runner.calls) == (1 if terminal == 'succeeded' else 0)
    assert fd_count() - before_fd == 0


# LLM: Event分别停在真实执行体和原ACK之前；证明先持久化并保留到确认，同Scope第二执行不准入。
# 函数用途: 验证版本脚本路径的互斥、退出证明单调保留及FD归还。
def test_versioned_scope_keeps_single_body_and_cleanup_proof_until_ack(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import concurrency_policy
    from agent_py_agent.agent.workflows.run_store import WorkflowRunStore

    retry_workflow_versions(tmp_path)
    engine = WorkflowEngine(tmp_path)
    engine.runner = CountingLocalRunner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    at_ack, allow_ack = threading.Event(), threading.Event()
    active, maximum, leases = [0], [0], []
    original_run, original_ack = engine.runner.run_script, WorkflowRunStore._ack_concurrency_release

    # LLM: 只安排调用时序，真实本地runner每次均委托，finally维护观察计数不改产品状态。
    # 函数用途: 阻止首执行体退出，观察竞争准入时是否发生重叠。
    def execute(*args, **kwargs):
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        entered.set()
        try:
            assert release.wait(15)
            return original_run(*args, **kwargs)
        finally:
            active[0] -= 1

    # LLM: ACK之前不伪造成功，也不清理任何原证明；事件放行后继续原持久化方法。
    # 函数用途: 暴露真实release_pending窗口供独立读取核对。
    def acknowledge(store, lease):
        leases.append(lease)
        at_ack.set()
        assert allow_ack.wait(15)
        return original_ack(store, lease)

    monkeypatch.setattr(engine.runner, 'run_script', execute)
    monkeypatch.setattr(WorkflowRunStore, '_ack_concurrency_release', acknowledge)
    before_fd = fd_count()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.run_workflow, 'daily', definition_version=1, concurrency_policy='skip_if_running')
        try:
            assert entered.wait(15)
            competitor = WorkflowEngine(tmp_path)
            competitor.runner = engine.runner
            second = competitor.run_workflow('daily', definition_version=1, concurrency_policy='skip_if_running')
            assert second['status'] == 'skipped' and active[0] == 1
            assert competitor.store.load_run(second['workflow_run_id']).status == 'skipped'
            release.set()
            assert at_ack.wait(15)
            lease = leases[0]
            run_json = tmp_path / 'data/workflow_runs' / lease.record['run_id'] / 'run.json'
            proof = json.loads(run_json.read_text())['concurrency_claim']
            assert proof['phase'] == 'release_pending' and proof['record'] == lease.record
            scope = concurrency_policy._scope(tmp_path, 'workflow', 'daily', lease.record['run_id'])
            key = concurrency_policy._claim_key(scope, lease)
            assert concurrency_policy._PENDING_RELEASES[key] == lease and active[0] == 0
        finally:
            release.set()
            allow_ack.set()
        first = future.result(timeout=15)
    assert first.status == 'succeeded' and maximum[0] == 1
    assert len(engine.runner.calls) == 1
    confirmed = json.loads(run_json.read_text())['concurrency_claim']
    assert confirmed['phase'] == 'released' and confirmed['record'] == proof['record']
    assert key not in concurrency_policy._PENDING_RELEASES
    assert fd_count() - before_fd == 0
