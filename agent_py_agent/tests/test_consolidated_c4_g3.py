"""Wave 3 consolidated tests for shard c4 (test_consolidated_c4_g3.py).

Sources:
- agent_py_agent/tests/test_v15_w1_asset_metadata.py
- agent_py_agent/tests/test_v15_w1_pitfalls.py
- agent_py_agent/tests/test_v15_w2_method_tree.py
- agent_py_agent/tests/test_v15_w2_tier1_catalog.py
- agent_py_agent/tests/test_v15_w3_compact_retention.py
- agent_py_agent/tests/test_v15_w3_inbound_indicators.py
- agent_py_agent/tests/test_v15_w3_sticky_tool_surface.py
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from inspect import signature
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.native_tool_protocol import resolve_native_tools
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import _initial_loop_loaded_tool_names
from agent_py_agent.agent.assets import asset_metadata as metadata
from agent_py_agent.agent.assets import pitfalls, topology
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.topology import build_asset_topology, extract_asset_closure
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.capability.inbound_indicators import extract_indicators
from agent_py_agent.agent.capability.method_tree import build_method_tree, render_method_catalog
from agent_py_agent.agent.capability.router import (
    _SKILL_METADATA_HEADER,
    _SKILL_USAGE_INSTRUCTIONS,
    CapabilityCard,
    CapabilityRouter,
)
from agent_py_agent.agent.conversation import compact_provider_surface as module
from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)
from agent_py_agent.tests.test_compactcall_websocket import _Probe, _setup

# === Consolidated from: agent_py_agent/tests/test_v15_w1_asset_metadata.py ===
# 验证资产元数据的确定性归一化，不读取用户状态。

# LLM: 输入包括无效成员；输出不得改变输入或依赖原始顺序。
# 函数用途: 验证归属去重、排序、安全标识和容量。
def test_member_of_mixed_sorted_capped():
    value = ['workflow:z', {'kind': 'skill', 'id': 'alpha'}, 'skill:alpha',
             {'kind': 'script', 'id': 'ignored'}, {'kind': [], 'id': 'bad'},
             'skill:../outside', 9, None, 'workflow:a:b']
    original = deepcopy(value)
    assert metadata.normalize_member_of(value) == [
        {'kind': 'skill', 'id': 'alpha'}, {'kind': 'workflow', 'id': 'z'}]
    assert value == original
    large = [f'workflow:w{i:02d}' for i in reversed(range(24))]
    assert metadata.normalize_member_of(large) == [
        {'kind': 'workflow', 'id': f'w{i:02d}'} for i in range(16)]


# LLM: 错误类型不是一条可用归属，不抛错或从字符串逐字推断。
# 函数用途: 验证归属错误类型容忍。
@pytest.mark.parametrize('value', [None, {}, 'skill:one', 4, True, ([],), [[], {}, '', 'skill:']])
def test_member_of_bad_input(value):
    assert metadata.normalize_member_of(value) == []


# LLM: 枚举与跨分片合同逐项一致，字符串顺序也是接口的一部分。
# 函数用途: 防止触发类型枚举漂移。
def test_indicator_enum_contract():
    assert metadata.INDICATOR_TYPES == (
        'ipv4', 'ipv6', 'domain', 'url', 'email', 'md5', 'sha1', 'sha256',
        'cve_id', 'hostname', 'port', 'process_name', 'file_path', 'alert_id',
        'monitor_alias', 'image_attachment')


# LLM: 四个字段各守类型和上限，不把提示字段解释为权限。
# 函数用途: 验证匹配器去重、清洗、保序和幂等。
def test_trigger_matchers_normalize():
    value = {'indicator_types': ['ipv4', 'unknown', 'ipv6', 'ipv4', {}],
             'monitor_ids': ['job48', '../bad', 'job48', 'job49', None],
             'intent_keywords': ['  ALPHA\nBeta  ', 'alpha beta', 'x' * 50, '\x00'],
             'alert_sources': ['source\nname', 'source name', 'x' * 90, 1],
             'ignored': ['never']}
    original = deepcopy(value)
    result = metadata.normalize_trigger_matchers(value)
    assert result == {'indicator_types': ['ipv4', 'ipv6'], 'monitor_ids': ['job48', 'job49'],
                      'intent_keywords': ['alpha beta', 'x' * 32],
                      'alert_sources': ['source name', 'x' * 64]}
    assert metadata.normalize_trigger_matchers(result) == result
    assert value == original


# LLM: 大输入不得突破各字段独立容量，也不能因字段类型错误报错。
# 函数用途: 验证匹配器容量及错误类型。
def test_trigger_matchers_limits_and_types():
    result = metadata.normalize_trigger_matchers({
        'indicator_types': list(metadata.INDICATOR_TYPES) * 3,
        'monitor_ids': [f'job{i}' for i in range(24)],
        'intent_keywords': [f'KEY{i}' for i in range(24)],
        'alert_sources': [f'source{i}' for i in range(24)]})
    assert result is not None
    assert [len(result[key]) for key in ('indicator_types', 'monitor_ids', 'intent_keywords', 'alert_sources')] == [16, 16, 16, 8]
    empty = {'indicator_types': [], 'monitor_ids': [], 'intent_keywords': [], 'alert_sources': []}
    for value in (None, 1, [], 'ipv4', {'indicator_types': 'ipv4', 'monitor_ids': {}, 'intent_keywords': 9}):
        assert metadata.normalize_trigger_matchers(value) == empty


# LLM: source_system 是用户定义字段，宿主管理键只从副本顶层剥离。
# 函数用途: 验证精确宿主键集与隔离拷贝。
def test_strip_host_metadata_contract_and_copy():
    expected = ('used_by', 'known_pitfalls', 'version', 'versions', 'version_info',
                'draft', 'evolution', 'hidden', 'composition', 'query_health', 'runtime_summary')
    assert expected == metadata.HOST_MANAGED_METADATA_KEYS
    value = dict.fromkeys(expected, True) | {'source_system': 'local', 'custom': {'version': 2}}
    original = deepcopy(value)
    result = metadata.strip_host_managed_metadata(value)
    assert result is not None
    clean, stripped = result
    assert clean == {'source_system': 'local', 'custom': {'version': 2}}
    assert stripped == sorted(expected)
    clean['custom']['version'] = 3
    assert value == original
    assert metadata.strip_host_managed_metadata(None) == ({}, [])


# === Consolidated from: agent_py_agent/tests/test_v15_w1_pitfalls.py ===
# 验证pitfall侧账本，不改动描述符或其他资产状态。

# LLM: 去重以清洗后的casefold内容计算，不以证据或时间重复造条目。
# 函数用途: 验证清洗、内容寻址ID及去重。
def test_pitfall_sanitize_and_deduplicate(tmp_path):
    entry = pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='  First\nSecond\x00  ',
        evidence_ref='receipt:one', source='user', recorded_at='2026-11-01T00:00:00+00:00')
    assert isinstance(entry, dict)
    assert entry['text'] == 'FirstSecond'
    assert entry['pitfall_id'] == hashlib.sha256(b'firstsecond').hexdigest()
    assert entry['source'] == 'user' and entry['evidence_ref'] == 'receipt:one'
    assert pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='FIRSTSECOND') is None
    path = tmp_path / 'data' / 'asset_evolution' / 'pitfalls' / 'script' / 'demo.json'
    data = json.loads(path.read_text())
    assert data['schema_version'] == 'asset_pitfalls.v1' and data['entries'] == [entry]
    assert not (tmp_path / 'scripts').exists()
    long = pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='z' * 300)
    assert len(long['text']) == 240
    assert pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='\x00\r\n') is None


# LLM: 存储FIFO最多二十条，读口倒序窗口不修改存储。
# 函数用途: 验证容量、默认读取五条及隔离。
def test_pitfall_fifo_and_newest_window(tmp_path):
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'empty') == []
    for number in range(24):
        pitfalls.add_known_pitfall(tmp_path, 'workflow', 'demo', text=f'entry {number}')
    entries = pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo', limit=99)
    assert isinstance(entries, list)
    assert [item['text'] for item in entries] == [f'entry {number}' for number in reversed(range(4, 24))]
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo') == entries[:5]
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo', limit=0) == []
    assert pitfalls.read_known_pitfalls(tmp_path, 'script', 'demo') == []


# LLM: 两个写者必须锁内重读，不能用锁外旧快照互相覆盖。
# 函数用途: 验证并发新增无丢条目。
def test_pitfall_concurrent_writers(tmp_path):
    barrier = Barrier(2)

    # LLM: 同时调用真实写入接口，不模拟存储结果。
    # 函数用途: 竞争写入一条pitfall。
    def add(text):
        barrier.wait(timeout=10)
        return pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text=text)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ('first', 'second')))
    assert all(isinstance(item, dict) for item in results)
    assert {row['text'] for row in pitfalls.read_known_pitfalls(tmp_path, 'script', 'demo')} == {'first', 'second'}


# LLM: 非法kind或用户路径不参与账本寻址。
# 函数用途: 验证侧账本身份边界。
@pytest.mark.parametrize('kind,asset_id', [('other', 'demo'), ('script', '../outside')])
def test_pitfall_identity_validation(tmp_path, kind, asset_id):
    with pytest.raises(ValueError):
        pitfalls.add_known_pitfall(tmp_path, kind, asset_id, text='text')
    with pytest.raises(ValueError):
        pitfalls.read_known_pitfalls(tmp_path, kind, asset_id)


# LLM: 只接受route3和user两个明确来源，不以正文推断来源。
# 函数用途: 验证来源枚举。
def test_pitfall_source_validation(tmp_path):
    with pytest.raises(ValueError):
        pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='text', source='unknown')


# === Consolidated from: agent_py_agent/tests/test_v15_w2_method_tree.py ===
# 描述符连边、反向父项和稳定技能名解析的合同回归。

# LLM: 输入仅为可序列化描述符，不生成资产执行内容。
# 函数用途: 建立可供真实拓扑函数消费的测试节点。
def node(kind, identifier, *, refs=None, metadata=None, name=None, enabled=True):
    return {"kind": kind, "id": identifier, "name": name or identifier, "refs": refs or {},
            "metadata": metadata or {}, "enabled": enabled}


# LLM: 所有边断言来自实际拓扑返回值，不以调用次数替代业务结果。
# 函数用途: 提取规范父子键以方便检查。
def pairs(rows):
    return {(edge["from"], edge["to"]) for edge in build_asset_topology(rows)["edges"]}


# LLM: 反向声明只能指向存在的技能或工作流，不信任缓存的used_by。
# 函数用途: 核对两种member_of形式、重复项和无效类型的容错。
def test_member_of_reverse_edges_ignore_invalid_and_stored_used_by():
    rows = [node("skill", "parent"), node("workflow", "flow"),
            node("script", "child", metadata={"member_of": ["skill:parent", {"kind": "workflow", "id": "flow"},
                "skill:parent", "script:child", "skill:absent", {}, 1]})]
    rows[2]["used_by"] = {"workflows": ["made-up"]}
    original = deepcopy(rows)
    result = build_asset_topology(rows)
    assert {(edge["from"], edge["to"]) for edge in result["edges"]} == {
        ("skill:parent", "script:child"), ("workflow:flow", "script:child")}
    child = next(row for row in result["nodes"] if row["key"] == "script:child")
    assert child["used_by"]["skills"] == ["parent"]
    assert child["used_by"]["workflows"] == ["flow"]
    assert rows == original


# LLM: 同名裸技能引用取优先来源，同优先级全部连边，不靠输入次序裁决。
# 函数用途: 锁定workspace覆盖owner及平级多候选语义。
def test_bare_skill_name_source_precedence_and_ties():
    rows = [node("workflow", "flow", refs={"skills": ["common"]}),
            node("skill", "workspace-a", name="common", metadata={"source": "workspace", "stable_id": "workspace:a"}),
            node("skill", "workspace-b", name="common", metadata={"source": "workspace", "stable_id": "workspace:b"}),
            node("skill", "owner-a", name="common", metadata={"source": "owner", "stable_id": "owner:a"})]
    expected = {("workflow:flow", "skill:workspace-a"), ("workflow:flow", "skill:workspace-b")}
    assert pairs(rows) == expected == pairs(list(reversed(rows)))
    rows[0]["refs"] = {"skills": ["owner-a"]}
    assert pairs(rows) == {("workflow:flow", "skill:owner-a")}


# LLM: 已发布监控描述符用正向refs触发，存储的反向引用永不产生边。
# 函数用途: 验证监控连边和监控非树节点。
def test_monitor_forward_edges_and_not_tree_node():
    rows = [node("workflow", "flow"), node("monitor", "job", refs={"workflows": ["flow"]}, metadata={"alias": "job48"})]
    assert pairs(rows) == {("monitor:job", "workflow:flow")}
    tree = build_method_tree(rows)
    assert set(tree["nodes"]) == {"workflow:flow"}
    assert tree["roots"] == ["workflow:flow"]


# LLM: 无法展示的父项不得通过member_of把可见子项吞掉。
# 函数用途: 覆盖隐藏、停用和不存在父项的折叠边界。
def test_invisible_member_of_parent_leaves_child_standalone():
    rows = [node("workflow", "hidden", metadata={"hidden": True}), node("workflow", "off", enabled=False),
            node("script", "child", metadata={"member_of": ["workflow:hidden", "workflow:off", "workflow:missing"]})]
    assert build_method_tree(rows)["roots"] == ["script:child"]


# LLM: 提取闭包保持线性安全，不改变已有根在最后的执行依赖顺序。
# 函数用途: 验证新增member_of边也用于闭包且输入used_by不能影响结果。
def test_closure_uses_member_of_dependencies():
    rows = [node("workflow", "parent"), node("script", "child", metadata={"member_of": ["workflow:parent"]})]
    assert [(row.kind, row.id) for row in extract_asset_closure(rows, "workflow:parent")] == [
        ("script", "child"), ("workflow", "parent")]


# LLM: 样本只含标准值对象，来源与逻辑身份显式给出，不读取磁盘资产。
# 函数用途: 构造可以走公共拓扑和闭包入口的最小描述符。
def _asset(kind, asset_id, *, name=None, refs=None, metadata=None, enabled=True, source_scope="owner"):
    return AssetDescriptor(asset_id, kind, name or asset_id, refs=refs or AssetRefs(),
                           metadata=metadata or {}, enabled=enabled, source_scope=source_scope)


# LLM: 主代理可用 stable_id 作实际图键，裸 id 仍是描述符身份，不能把二者强行等同。
# 函数用途: 构造与纯 method_tree 约定同形的规范节点表。
def _nodes(descriptors):
    nodes = {}
    for descriptor in descriptors:
        identity = descriptor.metadata.get("stable_id", descriptor.id) if descriptor.kind == "skill" else descriptor.id
        key = f"{descriptor.kind}:{identity}"
        nodes[key] = descriptor.to_dict() | {"key": key}
    return nodes


# LLM: 简单环枚举不是目录或投递闭包的工作，误调用时必须让测试明确失败。
# 函数用途: 提供不接受可变参数的环枚举防线。
def _no_cycles(selected, edges):
    pytest.fail("目录/闭包不得枚举全部简单环")


# LLM: member_of 是孩子向父的声明，边仍统一为父到子，重复和 refs 重叠不能多计。
# 函数用途: 锁住 skill/workflow 多父绑定和反向 used_by 的统一语义。
def test_member_of_reverses_edges_and_deduplicates_explicit_refs():
    skill = _asset("skill", "analysis", refs=AssetRefs(scripts=("collect",)))
    flow = _asset("workflow", "daily")
    child = _asset("script", "collect", metadata={"member_of": [
        {"kind": "skill", "id": "analysis"}, "workflow:daily", "skill:analysis"]})
    nested = _asset("workflow", "nested", metadata={"member_of": ["skill:analysis"]})
    graph = topology.build_asset_topology((child, flow, skill, nested))
    assert graph["edges"] == [
        {"from": "skill:analysis", "to": "script:collect", "relation": "uses_script"},
        {"from": "skill:analysis", "to": "workflow:nested", "relation": "uses_workflow"},
        {"from": "workflow:daily", "to": "script:collect", "relation": "uses_script"}]
    nodes = {row["key"]: row for row in graph["nodes"]}
    assert nodes["script:collect"]["used_by"] == AssetRefs(skills=("analysis",), workflows=("daily",)).to_dict()
    assert child.used_by == AssetRefs()
    assert graph == topology.build_asset_topology((nested, skill, flow, child))


# LLM: monitor 的触发边与其它依赖同源；stored used_by 无论位于哪层都不是连边依据。
# 函数用途: 验证 monitor 工作流/脚本引用和重新计算的反向引用。
def test_monitor_edges_and_stored_used_by_are_not_authoritative():
    monitor = _asset("monitor", "watch", refs=AssetRefs(workflows=("daily",), scripts=("collect",)))
    child = replace(_asset("script", "collect", metadata={"used_by": {"workflows": ["fake"]}}),
                    used_by=AssetRefs(workflows=("fake",)))
    graph = topology.build_asset_topology((monitor, child, _asset("workflow", "daily")))
    assert graph["edges"] == [
        {"from": "monitor:watch", "to": "script:collect", "relation": "uses_script"},
        {"from": "monitor:watch", "to": "workflow:daily", "relation": "triggers"}]
    nodes = {row["key"]: row for row in graph["nodes"]}
    assert nodes["script:collect"]["used_by"] == AssetRefs(monitors=("watch",)).to_dict()
    assert "workflow:fake" not in nodes
    assert child.used_by.workflows == ("fake",)


# LLM: 消费端在 W1 之前独立开发，缺键、错类型、坏 ID 与未知 kind 都按不存在处理。
# 函数用途: 确认坏 member_of 不抛错、不生成幽灵父节点。
@pytest.mark.parametrize("member_of", [None, "workflow:parent", {}, True, 7, [None, [], True, 7, ""],
    [{"kind": "script", "id": "parent"}], [{"kind": [], "id": "parent"}],
    [{"kind": "workflow", "id": []}], [{"kind": "workflow"}],
    ["workflow:../escape", "workflow:", "unknown:parent", "workflow:parent:extra"]])
def test_invalid_member_of_is_ignored(member_of):
    graph = topology.build_asset_topology((_asset("workflow", "parent"),
        _asset("script", "child", metadata={"member_of": member_of})))
    assert graph["edges"] == []
    assert {node["key"] for node in graph["nodes"]} == {"workflow:parent", "script:child"}


# LLM: 不可见父既不能折叠孩子，也不能因为 member_of 而生成 missing 依赖。
# 函数用途: 验证 hidden/disabled/absent 父和已经生成的 missing 父均不参与反向绑定。
@pytest.mark.parametrize("state", ["hidden", "disabled", "absent", "placeholder"])
def test_member_of_ignores_invisible_parents(state):
    descriptors = [_asset("script", "child", metadata={"member_of": ["workflow:parent"]})]
    if state == "hidden":
        descriptors.append(_asset("workflow", "parent", metadata={"hidden": True}))
    elif state == "disabled":
        descriptors.append(_asset("workflow", "parent", enabled=False))
    elif state == "placeholder":
        descriptors.insert(0, _asset("monitor", "watch", refs=AssetRefs(workflows=("parent",))))
    graph = topology.build_asset_topology(descriptors, root_key="script:child")
    assert [row["key"] for row in graph["nodes"]] == ["script:child"]
    assert graph["edges"] == []
    assert graph["nodes"][0]["used_by"] == AssetRefs().to_dict()


# LLM: 别名优先级来自 metadata.source，不从 stable_id 前缀或默认 source_scope 推断。
# 函数用途: 裸 skill 名只连最高存在来源，同级候选必须全部保留。
@pytest.mark.parametrize("sources", [
    ("builtin", "shared", "owner", "workspace"), ("builtin", "shared", "owner"),
    ("builtin", "shared"), ("builtin",)])
def test_bare_skill_name_resolves_source_priority_and_all_ties(sources):
    skills = [_asset("skill", f"copy-{source}", name="analysis", metadata={
        "source": source, "stable_id": f"{source}:analysis"}) for source in sources]
    best = sources[-1]
    skills.append(_asset("skill", "second", name="analysis", metadata={
        "source": best, "stable_id": f"{best}:second"}))
    nodes = _nodes([_asset("workflow", "daily", refs=AssetRefs(skills=("analysis",))), *skills])
    edges = topology._connect(nodes)
    targets = {f"skill:{best}:analysis", f"skill:{best}:second"}
    assert {edge["to"] for edge in edges} == targets
    assert all(nodes[key]["used_by"]["workflows"] == ["daily"] for key in targets)
    assert not any(node.get("missing") for node in nodes.values())


# LLM: 只有缺失或空 source 才回退 source_scope；身份前缀与默认 owner 不能覆盖实际来源。
# 函数用途: 验证 metadata.source 权威及旧描述符的回退来源。
@pytest.mark.parametrize("fallback", [None, ""])
def test_skill_source_authority_and_source_scope_fallback(fallback):
    skills = [_asset("skill", "first", name="analysis", source_scope="workspace", metadata={
        "stable_id": "workspace:analysis", "source": "builtin"}),
        _asset("skill", "second", name="analysis", source_scope="builtin", metadata={
            "stable_id": "builtin:analysis", "source": fallback})]
    # 旧描述符只提供 source_scope；只有这份才是真正的 owner 来源。
    skills.append(_asset("skill", "third", name="analysis", metadata={
        "stable_id": "legacy:analysis", "source": fallback}))
    nodes = _nodes([_asset("workflow", "daily", refs=AssetRefs(skills=("analysis",))), *skills])
    assert topology._connect(nodes) == [
        {"from": "workflow:daily", "to": "skill:legacy:analysis", "relation": "uses_skill"}]


# LLM: 准确 stable_id/图键查找绕过裸名优先级，返回传入表内的 key 而不是重拼 id。
# 函数用途: 验证显式来源引用、实际图键与 Unicode 逻辑身份。
@pytest.mark.parametrize("reference", ["shared:巡检", "skill:shared:巡检"])
def test_exact_skill_reference_returns_actual_key(reference):
    nodes = _nodes([_asset("workflow", "daily"),
        _asset("skill", "safe-shared", name="巡检", metadata={"source": "shared", "stable_id": "shared:巡检"}),
        _asset("skill", "safe-workspace", name="巡检", metadata={"source": "workspace", "stable_id": "workspace:巡检"})])
    nodes["workflow:daily"]["refs"]["skills"] = [reference]
    assert topology._connect(nodes) == [
        {"from": "workflow:daily", "to": "skill:shared:巡检", "relation": "uses_skill"}]
    assert nodes["skill:shared:巡检"]["used_by"]["workflows"] == ["daily"]


# LLM: bare id 和 name 都是规范表的别名；重新连接时不能累积或信任旧 used_by。
# 函数用途: 锁住实际 key 映射、别名去重和 _connect 重入行为。
def test_canonical_node_aliases_and_repeated_connect_ignore_stored_used_by():
    nodes = _nodes([_asset("workflow", "daily", refs=AssetRefs(skills=("safe-id", "analysis"))),
        replace(_asset("skill", "safe-id", name="analysis", metadata={"source": "owner", "stable_id": "owner:analysis"}),
                used_by=AssetRefs(workflows=("forged",)))])
    original = deepcopy(nodes)
    expected = [{"from": "workflow:daily", "to": "skill:owner:analysis", "relation": "uses_skill"}]
    assert topology._connect(nodes, stable_order=False) == expected
    assert topology._connect(nodes) == expected
    assert nodes["skill:owner:analysis"]["used_by"]["workflows"] == ["daily"]
    assert original["skill:owner:analysis"]["used_by"]["workflows"] == ["forged"]


# LLM: 真实目录里不同来源可能同 id，公共入口不能合并它们或拒绝合法的来源并存。
# 函数用途: 验证同 id 多来源的索引、最高来源边和根工作流闭包。
def test_public_topology_keeps_same_id_skills_from_distinct_sources():
    skills = [_asset("skill", "analysis", metadata={"source": source, "stable_id": f"{source}:analysis"})
              for source in ("builtin", "owner", "workspace")]
    flow = _asset("workflow", "daily", refs=AssetRefs(skills=("analysis",)))
    graph = topology.build_asset_topology([flow, *skills])
    assert {node["key"] for node in graph["nodes"]} == {
        "workflow:daily", "skill:builtin:analysis", "skill:owner:analysis", "skill:workspace:analysis"}
    assert graph["edges"] == [
        {"from": "workflow:daily", "to": "skill:workspace:analysis", "relation": "uses_skill"}]
    closure = topology.extract_asset_closure([flow, *skills], "workflow:daily")
    assert [(item.kind, item.metadata.get("source")) for item in closure] == [("skill", "workspace"), ("workflow", None)]


# LLM: SCC 与连边是目录复用的线性入口；稠密环图不能触发所有简单环的指数级枚举。
# 函数用途: 验证规范节点图的 SCC 可直接用于目录根选择。
def test_connect_and_components_do_not_enumerate_cycles(monkeypatch):
    monkeypatch.setattr(topology, "_cycles", _no_cycles)
    identifiers = tuple(f"flow-{index}" for index in range(12))
    nodes = _nodes([_asset("workflow", name, refs=AssetRefs(workflows=identifiers)) for name in identifiers])
    edges = topology._connect(nodes, stable_order=False)
    adjacency = {key: set() for key in nodes}
    for edge in edges:
        adjacency[edge["from"]].add(edge["to"])
    assert list(topology._components(adjacency)) == [set(nodes)]


# LLM: 新反向绑定也必须走原有依赖先行闭包，不引入枚举环或读取资产正文的旁路。
# 函数用途: 验证签名、返回类型、missing 机器码和只检查根可达依赖的原合同。
def test_closure_contract_and_member_of_dependency_order(monkeypatch):
    monkeypatch.setattr(topology, "_cycles", _no_cycles)
    assert list(signature(topology.build_asset_topology).parameters) == ["descriptors", "root_key"]
    assert list(signature(topology.extract_asset_closure).parameters) == ["descriptors", "root_key"]
    script = _asset("script", "collect", metadata={"member_of": ["workflow:daily"]})
    flow = _asset("workflow", "daily")
    unrelated = _asset("workflow", "unrelated", refs=AssetRefs(scripts=("missing",)))
    closure = topology.extract_asset_closure((flow, unrelated, script), "workflow:daily")
    assert isinstance(closure, tuple)
    assert all(isinstance(item, AssetDescriptor) for item in closure)
    assert [item.id for item in closure] == ["collect", "daily"]
    assert closure[0].used_by.workflows == ("daily",)
    for root, missing in (("workflow:unrelated", "script:missing"), ("workflow:absent", "workflow:absent")):
        with pytest.raises(topology.AssetClosureError) as error:
            topology.extract_asset_closure((flow, unrelated, script), root)
        assert error.value.error_code == "ASSET_DEPENDENCY_MISSING"
        assert error.value.missing_refs == [missing]


# LLM: 主代理预筛节点后，member_of 仍须沿父节点实际 key 连边，不能悄悄造出裸 ID 节点。
# 函数用途: 验证规范 skill 父键与可见性过滤协同工作。
def test_member_of_in_canonical_table_uses_visible_actual_skill_key():
    descriptors = [_asset("skill", "analysis", metadata={"source": "owner", "stable_id": "owner:analysis"}),
        _asset("skill", "hidden-copy", name="analysis", metadata={
            "source": "workspace", "stable_id": "workspace:analysis", "hidden": True}),
        _asset("script", "collect", metadata={"member_of": [None, "skill:analysis", {"kind": "script", "id": "bad"}]})]
    nodes = _nodes(descriptors)
    assert topology._connect(nodes) == [
        {"from": "skill:owner:analysis", "to": "script:collect", "relation": "uses_script"}]
    assert nodes["script:collect"]["used_by"]["skills"] == ["analysis"]
    assert set(nodes) == {"skill:owner:analysis", "skill:workspace:analysis", "script:collect"}


# LLM: 裸名不能因恰好存在同名 kind:id 键而绕过来源优先级。
# 函数用途: 锁住原有裸键与高优先级 name 别名并存时的解析。
def test_bare_skill_name_does_not_prefer_lower_source_actual_key():
    graph = topology.build_asset_topology([
        _asset("workflow", "daily", refs=AssetRefs(skills=("analysis",))),
        _asset("skill", "analysis", metadata={"source": "owner"}),
        _asset("skill", "workspace-copy", name="analysis", metadata={"source": "workspace"})])
    assert graph["edges"] == [
        {"from": "workflow:daily", "to": "skill:workspace-copy", "relation": "uses_skill"}]


# 最终 selection 顶层候选用例由主代理在整合 decision_candidates 后补在本文件，勿另建测试文件。


# === Consolidated from: agent_py_agent/tests/test_v15_w2_tier1_catalog.py ===
# Tier-1 的纯图、预算和公开索引不变量；不调用模型或执行资产。

# LLM: 样本仅声明资产元数据，不产生存储或执行副作用。
# 函数用途: 生成可独立修改的目录描述符。
def descriptor(kind, identifier, *, summary="", refs=None, metadata=None, enabled=True, source="owner"):
    return {"kind": kind, "id": identifier, "name": identifier, "summary": summary,
            "refs": refs or {}, "metadata": metadata or {}, "enabled": enabled, "source_scope": source}


# LLM: 使用审计明确的名称、ID和中文说明长度；规模数据不是生产常量。
# 函数用途: 建立重用户、有绑定和超规格三组预算输入。
def inventory(skills=20, workflows=50, scripts=100, *, bindings=True, monitors=5, distinct_categories=False):
    rows = [descriptor("skill", f"skill-name-{i:03d}", summary="方法说明来源核对与事件关联步骤验收" * 4,
                       metadata={"stable_id": f"owner:skill-name-{i:03d}", "source": "owner"}) for i in range(skills)]
    rows.extend(descriptor("workflow", f"workflow-id-{i:04d}", summary="工作流说明来源核对和状态不变量检查" * 3,
                           refs={"scripts": [f"fixed-script-{j:03d}" for j in range(i * 6, i * 6 + 6)]}
                           if bindings and i < 10 else {}) for i in range(workflows))
    rows.extend(descriptor("script", f"fixed-script-{i:03d}", metadata={"category": "indicator"}) for i in range(scripts))
    if bindings:
        for i in range(min(10, skills, workflows)):
            rows[i]["refs"] = {"workflows": [f"workflow-id-{i:04d}"]}
    rows.extend(descriptor("monitor", f"monitor-{i}", refs={"workflows": [f"workflow-id-{i:04d}"]},
                           metadata={"alias": f"job{i:03d}"}) for i in range(monitors))
    if distinct_categories:
        # L4按类目计数，只有类目本身也超出预算时才应进入L5；资产数不是档位阈值。
        for row in rows:
            if row["kind"] in {"workflow", "script"}:
                row["metadata"]["category"] = row["id"]
    return rows


# LLM: 统一使用路由器原始技能使用说明计入整个块的费用。
# 函数用途: 从描述符生成可核对的完整目录和阶梯测量值。
def catalog(rows):
    return render_method_catalog(rows, skill_usage_instructions=_SKILL_USAGE_INSTRUCTIONS,
                                 skill_header=_SKILL_METADATA_HEADER, bytes_per_token=4)


# LLM: 在用列表只影响易变区，不允许改变公开技能的字节或包含范围。
# 函数用途: 覆盖公开模式 prompt 和 in-use 独立性，包含未推荐方法。
def test_public_catalog_ignores_prompt_and_in_use_including_inclusion():
    cards = [CapabilityCard("skill:a", "skill", "Alpha", "alpha source", source="owner",
                            metadata={"stable_id": "owner:alpha"}),
             CapabilityCard("skill:z", "skill", "Zulu", "job48 source", source="owner",
                            metadata={"stable_id": "owner:zulu"}),
             CapabilityCard("skill:off", "skill", "Not recommended", "source", source="owner",
                            metadata={"stable_id": "owner:off", "recommended": False})]
    router = CapabilityRouter(extra_cards=cards)
    outputs = [router.render_skill_metadata_index(query=q, in_use_method_ids=used)
               for q in ("plain", "192.0.2.1 alert", "job48")
               for used in ((), ("owner:zulu", "owner:off", "script:owner:missing"))]
    assert len(set(outputs)) == 1
    assert "owner:off" not in outputs[0] and "本会话在用" not in outputs[0]
    assert outputs[0].index("owner:alpha") < outputs[0].index("owner:zulu")


# LLM: 直连绑定子项只在父徽标计数，所有可见技能仍是明确入口。
# 函数用途: 核对绑定折叠、共享计数和不可见父不隐藏子项。
def test_bound_children_shared_parent_counts_and_invisible_parent():
    rows = [descriptor("skill", "s", refs={"workflows": ["w"]}, metadata={"stable_id": "owner:s"}),
            descriptor("workflow", "w", refs={"scripts": ["x"]}),
            descriptor("workflow", "other", refs={"scripts": ["x"]}), descriptor("script", "x"),
            descriptor("workflow", "hidden", refs={"scripts": ["free"]}, metadata={"hidden": True}),
            descriptor("script", "free")]
    tree = build_method_tree(rows)
    assert set(tree["roots"]) == {"skill:owner:s", "workflow:other", "script:free"}
    assert tree["children"]["workflow:w"] == ["script:x"]
    text = catalog(rows)["text"]
    assert "(skill_id: owner:s) ⤷1" in text and "[workflow] other" in text and "⤷1" in text
    assert "[workflow] w:" not in text and "hidden" not in text
    assert "free" in text and "fixed-script" not in text


# LLM: SCC 只需线性分解，不能枚举简单环或以输入顺序决定根。
# 函数用途: 锁定无外部父循环的最小键及外部父存在时的折叠行为。
def test_cycle_minimum_root_and_external_parent():
    rows = [descriptor("workflow", "b", refs={"workflows": ["a"]}),
            descriptor("workflow", "a", refs={"workflows": ["b"]})]
    assert build_method_tree(rows)["roots"] == ["workflow:a"]
    rows.append(descriptor("skill", "s", refs={"workflows": ["b"]}, metadata={"stable_id": "owner:s"}))
    assert build_method_tree(rows)["roots"] == ["skill:owner:s"]


# LLM: 徽标只读显式别名和引用；隐藏组合只将目标重定向到基础工作流。
# 函数用途: 验证行内别名上限、无行目标和隐藏组合监控。
def test_monitor_badges_hidden_composition_and_unattached_targets():
    rows = [descriptor("skill", "s", refs={"workflows": ["bound"]}, metadata={"stable_id": "owner:s"}),
            descriptor("workflow", "bound"), descriptor("workflow", "base"), descriptor("script", "alone"),
            descriptor("workflow", "monitor-m", metadata={"hidden": True, "composition": {"base_workflow_id": "base"}})]
    rows.extend(descriptor("monitor", f"m{i}", refs={"workflows": ["monitor-m"]},
                           metadata={"alias": f"job{i}"}) for i in range(4))
    rows.extend([descriptor("monitor", "child", refs=[{"kind": "workflow", "id": "bound"}], metadata={"alias": "child-job"}),
                 descriptor("monitor", "drawer", refs={"scripts": ["alone"]}, metadata={"alias": "drawer-job"})])
    result = catalog(rows)
    assert "⏰job0,job1(+2)" in result["text"]
    assert "[⏰] child-job→workflow:bound" in result["text"]
    assert "drawer-job→script:alone" in result["text"]
    assert "[workflow] monitor-m" not in result["text"]
    assert all(node["kind"] != "monitor" for node in build_method_tree(rows)["nodes"].values())


# LLM: 三组规模使用同一估算器；首个满足预算的级别才可采用。
# 函数用途: 证明重用户、最坏和超规格输入都在完整2500-token信封内。
@pytest.mark.parametrize("scale,allowed", [("gate", {"L1"}), ("worst", {"L2", "L3"}), ("beyond", {"L5"})])
def test_budget_ladder_first_fitting_level(scale, allowed):
    if scale == "gate":
        rows = inventory()
    elif scale == "worst":
        rows = inventory(30, 50, 100, bindings=False, monitors=0)
    else:
        rows = inventory(30, 300, 600, bindings=False, monitors=0, distinct_categories=True)
    result = catalog(rows)
    assert result["level"] in allowed
    assert result["tokens"] <= 2500
    assert result["tokens"] == (len(result["text"].encode()) + 3) // 4
    assert all(cost > 2500 for cost in result["level_tokens"][:int(result["level"][1:])])
    assert _SKILL_USAGE_INSTRUCTIONS in result["text"]
    assert "scripts/workflows(action=list)" in result["text"]


# LLM: 同一类目的库存即使超规格也不能绕过已适配的L4，避免按资产数量硬编码档位。
# 函数用途: 保留原失败输入，证明先适配原则与L5压力用例同时成立。
def test_beyond_scale_single_category_keeps_first_fitting_level():
    result = catalog(inventory(30, 300, 600, bindings=False, monitors=0))
    assert result["level"] == "L4"
    assert result["level_tokens"][4] <= 2500
    assert all(cost > 2500 for cost in result["level_tokens"][:4])
    assert result["tokens"] == (len(result["text"].encode()) + 3) // 4


# LLM: 所有可变调用信息和版本观察均不得进入固定目录。
# 函数用途: 验证排序、原输入不变和库存事件仅触发一次字节变化。
def test_inventory_only_order_and_event_changes():
    rows = inventory(2, 3, 4, monitors=0)
    before = deepcopy(rows)
    text = catalog(rows)["text"]
    assert catalog(list(reversed(rows)))["text"] == text and rows == before
    for row in rows:
        row.update(revision=991, version="v999", runtime_summary={"last_run_at": "tomorrow"})
        row["metadata"].update(known_pitfalls=["volatile-note"], trigger_matchers={"monitor_ids": ["later"]})
    assert catalog(rows)["text"] == text
    rows.append(descriptor("script", "added"))
    changed = catalog(rows)["text"]
    assert changed != text and changed == catalog(rows)["text"]


# LLM: 清理展示文本控制字符但不解释或执行说明中的指令。
# 函数用途: 阻止多行说明创建额外目录结构，并确定类目回退规则。
def test_summaries_single_line_and_category_slug():
    rows = [descriptor("skill", "s", summary="first\n### another\r\x00\u2028last", metadata={"stable_id": "owner:s"}),
            descriptor("script", "one", metadata={"category": "SOURCE_QUERY"}), descriptor("script", "two")]
    rows[2]["tags"] = ["event-correlation"]
    text = catalog(rows)["text"]
    assert "\x00" not in text and "\r" not in text and "\u2028" not in text
    assert not any(line.startswith("### another") for line in text.splitlines())
    assert "[script·source_query] 1: one" in text and "[script·event-correlation] 1: two" in text


# LLM: 极大技能集合允许既有省略语义，整块仍必须有完整用法和预算。
# 函数用途: 为超过150个技能和长标识符保留有界退出路径。
def test_many_skills_omit_whole_lines_and_bound_envelope():
    rows = inventory(400, 1, 1, bindings=False, monitors=0)
    result = catalog(rows)
    assert result["tokens"] <= 2500 and "未显示" in result["text"]
    assert _SKILL_USAGE_INSTRUCTIONS in result["text"]


# LLM: 路由入口必须消费同一冻结绑定，不能只有纯函数测试通过；样本仅使用隔离Store。
# 函数用途: 验证真实公开目录的技能折叠、方法标题和完整预算。
@pytest.mark.parametrize("name,reverse", [("review", False), ("中文方法", True)])
def test_router_uses_frozen_bindings_and_fixed_method_header(tmp_path, name, reverse):
    from types import MethodType

    from agent_py_agent.agent.assets import skill_asset_id
    from agent_py_agent.tests.test_v15_w2_l1_skill_search import _script, _surface, _workflow

    agent = _surface(tmp_path, name=name, refs={} if reverse else {"workflows": ["bound"]})
    metadata = {"member_of": [{"kind": "skill", "id": skill_asset_id(name)}]} if reverse else {}
    _workflow(agent, "bound", scripts=("child",), metadata=metadata)
    _script(agent, "child")

    # LLM: 生产路由只接受宿主绑定的快照提供者，不从任意回调或路径猜Owner。
    # 函数用途: 给测试Agent提供有明确宿主身份的快照方法。
    def snapshot_for(self):
        return self._current_skill_snapshot

    agent.current_skill_snapshot = MethodType(snapshot_for, agent)
    router = CapabilityRouter(skill_snapshot_provider=agent.current_skill_snapshot)
    text = router.render_skill_metadata_index()
    assert f"(skill_id: owner:{name}) ⤷1" in text
    assert "### 固定方法（工作流 / 脚本）" in text
    assert "[workflow] bound" not in text and "[script·" not in text
    assert (len(text.encode("utf-8")) + 3) // 4 <= 2500


# LLM: W8错误字段必须在同一生产解析口按缺失处理，不能让未哈希类型中断目录或L1。
# 函数用途: 复现并锁定monitor列表引用中错误kind类型的容错行为。
@pytest.mark.parametrize("bad_kind", [[], {}])
def test_monitor_wrong_type_ref_kind_is_ignored(bad_kind):
    rows = [descriptor("workflow", "w"),
            descriptor("monitor", "m", refs=[{"kind": bad_kind, "id": "w"}], metadata={"alias": "job"})]
    failure = None
    tree = None
    try:
        tree = build_method_tree(rows)
    except TypeError as exc:
        failure = str(exc)
    assert failure is None, failure
    assert tree["monitors"] == []
    assert tree["roots"] == ["workflow:w"]


# LLM: 同一W8容错合同也覆盖别名，错误类型不是可展示的别名。
# 函数用途: 防止把列表、字典或数字强制转成监控徽标。
@pytest.mark.parametrize("bad_alias", [["wrong"], {"wrong": 1}, 1])
def test_monitor_wrong_type_alias_is_ignored(bad_alias):
    rows = [descriptor("workflow", "w"),
            descriptor("monitor", "m", refs=[{"kind": "workflow", "id": "w"}], metadata={"alias": bad_alias})]
    assert build_method_tree(rows)["monitors"] == []


# LLM: 隐藏标记的意义不随无关固定资产出现而变化，公开技能回退也必须遵守同一可见性。
# 函数用途: 成对验证无资产回退和方法树路径都不泄露隐藏技能。
@pytest.mark.parametrize("with_asset", [False, True])
def test_public_hidden_skill_absent_with_or_without_fixed_assets(with_asset):
    cards = [CapabilityCard("skill:hidden", "skill", "hidden-method-name", "private summary", source="owner",
                            metadata={"stable_id": "owner:hidden", "hidden": True}),
             CapabilityCard("skill:visible", "skill", "visible", "public summary", source="owner",
                            metadata={"stable_id": "owner:visible"})]
    if with_asset:
        cards.append(CapabilityCard("script:owner:alone", "script", "alone", "source", source="owner",
                                    metadata={"stable_id": "script:owner:alone", "asset_id": "alone"}))
    text = CapabilityRouter(extra_cards=cards).render_skill_metadata_index()
    assert "owner:hidden" not in text and "hidden-method-name" not in text
    assert "owner:visible" in text


# LLM: 包的在用标记和说明只属于包区，不能通过共享预算使固定Skill目录退成单项兜底。
# 函数用途: 覆盖默认、常用token窗口与硬字符信封下的包/Skill预算隔离。
@pytest.mark.parametrize("window", [None, 128000, 1000000])
def test_package_in_use_does_not_collapse_or_change_public_skill_block(window):
    cards = [CapabilityCard(f"skill:{i}", "skill", f"method-{i:03}", "来源核对和状态不变量检查" * 100,
                            source="owner", metadata={"stable_id": f"owner:method-{i:03}"})
             for i in range(20)]
    package = CapabilityCard("capability:midturn-method", "capability_package", "midturn-method",
                             "把长篇材料整理成分镜", metadata={"stable_id": "capability:midturn-method",
                             "package_id": "midturn-method"})
    router = CapabilityRouter(extra_cards=[*cards, package])
    outputs = [router.render_skill_metadata_index(context_window_tokens=window, in_use_method_ids=used)
               for used in ((), ("capability:midturn-method",))]
    skill_blocks = [text.split("# Available Skills", 1)[1] for text in outputs]
    assert skill_blocks[0] == skill_blocks[1]
    assert skill_blocks[0].count("(skill_id:") > 1
    assert "midturn-method（本会话在用）" in outputs[1]
    assert all(len(text) <= 12000 for text in outputs)
    if window is not None:
        assert all((len(text.encode("utf-8")) + 3) // 4 <= window * 2 // 100 for text in outputs)


# === Consolidated from: agent_py_agent/tests/test_v15_w3_compact_retention.py ===
# W3 推荐尾段压缩保留：单模型、选择模式、原IR去重和只读恢复接缝。

# LLM: 仅冻结可选选择载体的分支，不运行决策模型；原快照、PromptBuilder和resolver仍是真实对象。
# 函数用途: 为两种模型模式准备可重复的压缩展示输入。
def setup_surface(tmp_path, monkeypatch, selected):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, False)

    # LLM: 使用原工具快照，只替代本测试不涉及的能力选择，不能制造新工具或执行权。
    # 函数用途: 控制选择载体是否为空。
    def presentation(host, surface, snapshot, protocol):
        return SimpleNamespace(tool_snapshot=snapshot, selection=object() if selected else None,
                               selected_skill_ids=None, required_skill_ids=())

    monkeypatch.setattr(module, '_compact_capability_presentation', presentation)
    context = RuntimeContextRequest('8.8.8.8', [], False, 'conversation')
    return agent, module.ConversationCompactModelSurface(thread_id=thread.thread_id, presentation_context=context,
                                                         context_scope='conversation')


# LLM: 通过真实推荐和builder生成当前唯一尾段，不从历史正文猜方法或借用旧section。
# 函数用途: 验证single-model和selection都保留当前Tier-2。
@pytest.mark.parametrize('selected', [False, True], ids=['single-model', 'selection'])
def test_compact_keeps_current_tier2_in_both_modes(tmp_path, monkeypatch, selected):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    agent, surface = setup_surface(tmp_path, monkeypatch, selected)
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('query-ip', 'script', '地址查询',
        metadata={'trigger_matchers': {'indicator_types': ['ipv4']}}), 'print("never executed")')
    prepared = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3')
    recommendations = [text for source, text in prepared.volatile_sections if source == 'prompt.tool_recommendations']
    assert len(recommendations) == 1 and recommendations[0].count('## 本轮推荐的固定方法') == 1
    assert 'query-ip' in recommendations[0]
    assert '## 本轮推荐的固定方法' not in prepared.stable_prompt_prefix
    messages = module.conversation_compact_provider_messages('', 0, [], volatile_sections=prepared.volatile_sections)
    assert str(messages).count('## 本轮推荐的固定方法') == 1


# LLM: layout禁止同源重复；连续两次准备各取当前结构化section，不把上次展示留在新对象。
# 函数用途: 验证最新section唯一性及旧对象不被热改。
@pytest.mark.parametrize('selected', [False, True], ids=['single-model', 'selection'])
def test_compact_keeps_only_latest_structured_section(tmp_path, monkeypatch, selected):
    agent, surface = setup_surface(tmp_path, monkeypatch, selected)

    versions = iter(('OLD-RECOMMENDATION', 'LATEST-RECOMMENDATION'))

    # LLM: 每次只提供一个合法同源section，展示变化来自新的宿主准备输入。
    # 函数用途: 连续提供旧、新两版结构化layout。
    def parent_prompt(host, request, params):
        return CacheStructuredPrompt('fixed-prefix', '', volatile_sections=(
            ('prompt.tool_recommendations', next(versions)),
            ('prompt.persona_updates', 'persona-update')))

    monkeypatch.setattr(module, '_compact_parent_prompt', parent_prompt)
    previous = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3-old')
    prepared = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3')
    assert prepared.volatile_sections == (('prompt.tool_recommendations', 'LATEST-RECOMMENDATION'),
                                         ('prompt.persona_updates', 'persona-update'))
    assert 'OLD-RECOMMENDATION' not in str(module.conversation_compact_provider_messages('', 0, [],
        volatile_sections=prepared.volatile_sections))
    assert previous.volatile_sections[0] == ('prompt.tool_recommendations', 'OLD-RECOMMENDATION')


@pytest.mark.parametrize(('supplied', 'scope'), [
    (True, 'conversation'), (False, 'isolated'), (True, 'isolated'),
    (False, 'control_plane'), (True, 'control_plane'),
])
def test_compact_rejected_or_isolated_surface_strips_recommendations_but_keeps_persona(
    tmp_path, monkeypatch, supplied, scope,
):
    """拒绝分支既清空 builder 的输入，也过滤自定义 renderer 回传的推荐；人格更新不受影响。"""
    agent, surface = setup_surface(tmp_path, monkeypatch, selected=False)
    surface = replace(surface, context_scope=scope, capability_presentation=object() if supplied else None)
    requests = []

    def parent_prompt(host, request, params):
        requests.append(request)
        return CacheStructuredPrompt('fixed-prefix', '', volatile_sections=(
            ('prompt.tool_recommendations', 'REJECTED-RECOMMENDATION'),
            ('prompt.persona_updates', 'PERSONA-UPDATE')))

    monkeypatch.setattr(module, '_compact_parent_prompt', parent_prompt)
    prepared = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3-rejected')
    assert requests[0].user_prompt == ''
    assert requests[0].tools.tool_recommendations_section == ''
    assert prepared.volatile_sections == (('prompt.persona_updates', 'PERSONA-UPDATE'),)
    assert prepared.stable_prompt_prefix == 'fixed-prefix'
    messages = module.conversation_compact_provider_messages('', 0, [], volatile_sections=prepared.volatile_sections)
    assert 'REJECTED-RECOMMENDATION' not in str(messages)
    assert 'PERSONA-UPDATE' in str(messages)


# LLM: 原IR合同是同文本不重复、空文本不撤销旧事实；此测试明确记录既有语义，不修改只读模块。
# 函数用途: 验证同输入去重、变化追加、空段忽略和原输入不变。
def test_runtime_facts_repeat_change_and_empty_semantics_are_preserved():
    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history

    params = SimpleNamespace(tool_ir_history=[UserTurn('原始任务')], task_attributes={}, compact_context=None)
    first_prompt = CacheStructuredPrompt('fixed', '', volatile_sections=(('prompt.tool_recommendations', 'SAME'),))
    provider_prompt, first = project_native_prompt_history(params, first_prompt)
    assert len(params.tool_ir_history) == 1
    params.tool_ir_history = first
    provider_prompt, repeated = project_native_prompt_history(params, first_prompt)
    assert repeated == first
    changed_prompt = CacheStructuredPrompt('fixed', '', volatile_sections=(('prompt.tool_recommendations', 'LATEST'),))
    provider_prompt, changed = project_native_prompt_history(params, changed_prompt)
    assert changed[-1] == RuntimeFactsTurn('LATEST', source='prompt.tool_recommendations')
    params.tool_ir_history = changed
    provider_prompt, empty = project_native_prompt_history(params, CacheStructuredPrompt('fixed', '',
        volatile_sections=(('prompt.tool_recommendations', ''),)))
    assert empty == changed
    assert [turn.text for turn in empty if isinstance(turn, RuntimeFactsTurn)] == ['SAME', 'LATEST']


# LLM: 只读恢复模块从冻结prompt_input取唯一当前推荐，不重新读库存或调用选择模型。
# 函数用途: 验证恢复摘要接缝保持一份最新推荐，旧IR仍保持原件。
def test_readonly_recovery_surface_keeps_one_current_section():
    from agent_py_agent.agent.agent_core.compact_request_recovery import _summary_surface
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput
    from agent_py_agent.tests.test_compact_active_projection import _fixture

    context, params, frozen, retained = _fixture()
    prompt = PromptRenderInput(system_prompt='fixed', memory_text='', owner_scope='', dynamic='', workspace_context='',
        injection_fragments=(), tool_catalog='', recommendations='LATEST-RECOMMENDATION', execution_facts='',
        user_prompt='继续', task_and_transcript='继续', native_tool_use=True)
    frozen = replace(frozen, prompt_input=prompt, native_tools=(), system_instruction='system',
                     tool_ir_history=(*frozen.tool_ir_history, RuntimeFactsTurn('OLD-RECOMMENDATION', source='prompt.tool_recommendations')))
    surface = _summary_surface(frozen)
    assert surface.volatile_sections == (('prompt.tool_recommendations', 'LATEST-RECOMMENDATION'),)
    assert frozen.tool_ir_history[-1].text == 'OLD-RECOMMENDATION'


# === Consolidated from: agent_py_agent/tests/test_v15_w3_inbound_indicators.py ===
# W3 入站指标合同：仅解析给定值，不读附件、不做网络请求。

# LLM: 测试只投影结果，不改变被测输入或来源。
# 函数用途: 取得某种指标的值供精确断言。
def values(rows, kind):
    return [row['value'] for row in rows if row['type'] == kind]


# LLM: 有效IP须完整校验，保留范围标记且不将版本解释为地址。
# 函数用途: 验证地址、保留范围和典型误报。
@pytest.mark.parametrize('text,expected', [
    ('8.8.8.8', ['8.8.8.8']), ('192.168.2.1', ['192.168.2.1']),
    ('999.1.1.1', []), ('v1.2.3.4', []), ('version 1.2.3.4', []),
    ('version: 1.2.3.4', []), ('1.2.3.4.5', []),
])
def test_ipv4_validation_and_versions(text, expected):
    rows = extract_indicators(text)
    assert values(rows, 'ipv4') == expected
    for row in rows:
        if row['type'] == 'ipv4':
            assert row['reserved'] is (text == '192.168.2.1')


# LLM: 文件扩展名是有限误报排除，不把普通域名及其子域一并排除。
# 函数用途: 验证域名识别及文件名否定对照。
@pytest.mark.parametrize('text,expected', [
    ('example.com', ['example.com']), ('api.example.org', ['api.example.org']),
    ('report.json main.py README.md data.csv build.js', []),
    ('image.png archive.tar.gz', []),
])
def test_domains_ignore_file_names(text, expected):
    assert values(extract_indicators(text), 'domain') == expected


# LLM: 来源和首次出现顺序来自给定文本，不按类型排序或去网络核验。
# 函数用途: 覆盖URL、email、摘要、CVE和IPv6的完整值。
def test_url_email_hashes_cve_and_ipv6():
    md5, sha1, sha256 = 'a' * 32, 'b' * 40, 'c' * 64
    text = f'https://example.com/a?q=1 user@example.org {md5} {sha1} {sha256} CVE-2024-12345 2001:db8::1'
    rows = extract_indicators(text)
    for kind, expected in [('url', 'https://example.com/a?q=1'), ('email', 'user@example.org'),
                           ('md5', md5), ('sha1', sha1), ('sha256', sha256),
                           ('cve_id', 'CVE-2024-12345'), ('ipv6', '2001:db8::1')]:
        assert values(rows, kind) == [expected]
    assert all(row['source'] == 'text' for row in rows)
    assert values(extract_indicators('a' * 31 + ' ' + 'b' * 41), 'md5') == []
    assert values(extract_indicators('a' * 31 + ' ' + 'b' * 41), 'sha1') == []


# LLM: 图片只消费元数据；OCR必须由调用方给出，不能隐式读取文件或运行识别。
# 函数用途: 验证纯图片、OCR和跨来源去重。
def test_image_only_and_adapter_ocr_are_scanned_without_file_access():
    attachment = {'media_type': 'image/png', 'name': 'alert.png', 'path': '/must/not/read', 'sha256': 'f' * 64}
    rows = extract_indicators('', attachments=[attachment], ocr_texts=['8.8.8.8 job48'])
    assert values(rows, 'image_attachment') == ['f' * 64]
    assert values(rows, 'ipv4') == ['8.8.8.8']
    assert next(row for row in rows if row['type'] == 'ipv4')['source'] == 'ocr:0'
    assert values(rows, 'monitor_alias') == ['job48']
    assert attachment['path'] == '/must/not/read'


# LLM: 扫描预算按UTF-8字节累计覆盖文本和OCR，不能靠多个字段绕过。
# 函数用途: 验证64KiB截断及多字节边界。
def test_scan_byte_cap_applies_to_all_text_sources():
    assert values(extract_indicators('x' * 65536 + ' 8.8.8.8'), 'ipv4') == []
    assert values(extract_indicators('汉' * 10 + ' 8.8.8.8', max_scan_bytes=30), 'ipv4') == []
    assert values(extract_indicators('x' * 32, ocr_texts=['8.8.8.8'], max_scan_bytes=32), 'ipv4') == []
    assert extract_indicators('8.8.8.8', max_scan_bytes=0) == []


# LLM: 同类型同值按首次观察去重，后来的来源不能改写首次来源。
# 函数用途: 验证确定顺序、去重及给定结构化标签。
def test_first_seen_order_and_structured_indicator_labels():
    rows = extract_indicators('8.8.8.8 1.1.1.1 8.8.8.8', ocr_texts=['1.1.1.1'])
    assert values(rows, 'ipv4') == ['8.8.8.8', '1.1.1.1']
    assert rows == extract_indicators('8.8.8.8 1.1.1.1 8.8.8.8', ocr_texts=['1.1.1.1'])
    labeled = extract_indicators('hostname=db01 port=443 process_name=worker file_path=/var/log/app alert_id=evt-9 monitor_alias=job48')
    for kind, value in [('hostname', 'db01'), ('port', '443'), ('process_name', 'worker'),
                        ('file_path', '/var/log/app'), ('alert_id', 'evt-9'), ('monitor_alias', 'job48')]:
        assert value in values(labeled, kind)


# === Consolidated from: agent_py_agent/tests/test_v15_w3_sticky_tool_surface.py ===
# W3 粘性工具：真实线程CAS、native resolver与强制Compact，供应商仅本地替身。

# LLM: 只有pytest根内有样本资产；脚本正文是未执行的标记，不借外部环境提供资格。
# 函数用途: 建立真实线程和一个可调用脚本。
def setup_case(tmp_path, monkeypatch, *, asset=True):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_default_deferral_enabled = True
    if asset:
        ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('fixed-query', 'script', '固定查询'), 'raise AssertionError("never execute")')
    return agent, thread


# LLM: 每次重建本轮参数，工具顺序只能来自持久线程与原resolver，不能复用上一轮可变集合。
# 函数用途: 沿真实初始加载接缝生成native工具表。
def next_turn(agent, thread, *, allowed=None, scope='conversation'):
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    attrs = {'conversation_thread_id': thread.thread_id}
    original = SimpleNamespace(source='user', context_scope=scope, allowed_tools=allowed, task_attributes=attrs)
    seed = SimpleNamespace(effective_contract_snapshot=None, tool_runtime_snapshot=snapshot)
    loaded = _initial_loop_loaded_tool_names(agent, original, seed, SimpleNamespace(loaded_tool_names=set()))
    params = ToolLoopExecuteParams(user_prompt='继续', memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section='', tool_recommendations_section='', tool_context=[], effective_on_chunk=None,
        allowed_tools=allowed, write_boundary=None, task_attributes=attrs, request_id='w3', run_id='w3', task_id='',
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[], loaded_tool_names=loaded,
        context_scope=scope, tool_runtime_snapshot=snapshot, tool_protocol_snapshot=ToolProtocolSnapshot(
            'w3', 'native', ProviderToolCapability('test', 'local://w3', 'test', False, True, 'test')))
    return params, resolve_native_tools(agent, params)


# LLM: 比较完整数组序列化字节，不只比较工具集合；资产删除不能回收已发现的schema。
# 函数用途: 验证三轮、重建参数与资产删除后的原生前缀不变。
def test_turns_one_to_three_persist_both_names_and_never_unmount(tmp_path, monkeypatch):
    agent, thread = setup_case(tmp_path, monkeypatch)
    first_params, first = next_turn(agent, thread)
    current = agent.conversation_store.threads.require(thread.thread_id)
    assert current.loaded_tool_names == ('scripts', 'workflows')
    assert current.metadata['pinned_tool_names'] == ['scripts', 'workflows']
    assert current.metadata['discovered_tool_history'] == ['scripts', 'workflows']
    assert {'scripts', 'workflows'} <= first_params.loaded_tool_names
    second = next_turn(agent, thread)[1]
    ScriptStore(agent.home_paths.owner_home_dir).delete_script('fixed-query')
    third = next_turn(agent, thread)[1]
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False) == json.dumps(third, ensure_ascii=False)
    assert [row['name'] for row in first][-2:] == ['scripts', 'workflows']
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts', 'workflows']


# LLM: 正式tool_search只发现名称，执行权仍由快照决定；持久化和下一轮恢复必须保序。
# 函数用途: 验证新发现恰好追加一个工具而不插入旧前缀。
def test_real_tool_search_appends_exactly_one_and_survives_next_turn(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools

    agent, thread = setup_case(tmp_path, monkeypatch)
    params, before = next_turn(agent, thread)
    assert 'package_build' not in {row['name'] for row in before}
    outcome = agent.tools.tools['tool_search'].execute({'names': ['package_build']})
    record = {'tool': 'tool_search', 'tool_round': 1, 'tool_result_envelope': outcome.result_envelope}
    _load_discovered_tools(params, record)
    persist_discovered_tools(agent, params, record)
    after = resolve_native_tools(agent, params)
    assert after == [*before, next(row for row in after if row['name'] == 'package_build')]
    assert next_turn(agent, thread)[1] == after
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ('scripts', 'workflows', 'package_build')


# LLM: 走正式Compact提交与线程重载，仅供应商响应是固定本地返回，不联网。
# 函数用途: 验证强制压缩后两个固定资产工具仍活跃且wire不变。
def test_forced_compaction_keeps_asset_pins_active(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
    )

    agent, thread = setup_case(tmp_path, monkeypatch)
    params, before = next_turn(agent, thread)
    _Probe(monkeypatch, False, live=False)
    current = agent.conversation_store.threads.require(thread.thread_id)
    result = prepare_conversation_context(agent, agent.conversation_store, current,
        options=ConversationCompactOptions(force=True, model_surface=ConversationCompactModelSurface(
            thread_id=thread.thread_id, loaded_tool_names=('scripts', 'workflows'))))
    assert result.compacted and result.thread.compact_generation == 1
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert stored.loaded_tool_names == ('scripts', 'workflows')
    assert stored.metadata['pinned_tool_names'] == ['scripts', 'workflows']
    assert next_turn(agent, thread)[1] == before


# LLM: 子代理身份使用正式线程本地context，隔离范围和空资产不能由拥有工具本身变成挂载资格。
# 函数用途: 验证没有资产、隔离/task_local与子代理均不持久预挂载。
@pytest.mark.parametrize('case', ['empty', 'task_local', 'isolated', 'subagent'])
def test_ineligible_contexts_do_not_mount(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent, thread = setup_case(tmp_path, monkeypatch, asset=case != 'empty')
    previous = set_current_subagent_context(agent, run_id='child') if case == 'subagent' else None
    try:
        params, wire = next_turn(agent, thread, scope=case if case in {'task_local', 'isolated'} else 'conversation')
        assert not {'scripts', 'workflows'} & params.loaded_tool_names
        assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ()
    finally:
        if previous is not None:
            restore_current_subagent_context(agent, previous)


# LLM: 白名单交集和模型可见资格都不能被持久pin或历史绕过。
# 函数用途: 验证只允许scripts时不挂workflows，之后收紧权限不泄露旧工具。
def test_allowed_tools_only_reduce_mount_and_execution_surface(tmp_path, monkeypatch):
    agent, thread = setup_case(tmp_path, monkeypatch)
    params, wire = next_turn(agent, thread, allowed=['tool_search', 'scripts'])
    assert params.loaded_tool_names == {'scripts'}
    assert {row['name'] for row in wire} == {'tool_search', 'scripts'}
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts']
    params, wire = next_turn(agent, thread, allowed=['tool_search'])
    assert {row['name'] for row in wire} == {'tool_search'}
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts']
