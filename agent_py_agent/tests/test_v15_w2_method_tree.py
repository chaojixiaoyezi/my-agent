"""描述符连边、反向父项和稳定技能名解析的合同回归。"""
from copy import deepcopy
from dataclasses import replace
from inspect import signature

import pytest

from agent_py_agent.agent.assets import topology
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs
from agent_py_agent.agent.assets.topology import build_asset_topology, extract_asset_closure
from agent_py_agent.agent.capability.method_tree import build_method_tree


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
