"""W3 动态推荐：真实描述符、拓扑和owner侧文件，零模型/网络。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.capability import turn_recommendations as module


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
