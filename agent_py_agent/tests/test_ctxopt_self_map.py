# LLM: 仅消费真实registry和冻结的授权快照，禁止刷新库存或读取Skill正文。
# 模块用途: 保护动态能力族计数、完整性标识与Owner/子集隔离。
"""能力族计数沿真实registry和当轮快照；不读Skill正文或刷新库存。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.skill_snapshot import (
    SkillLoadError,
    SkillSnapshot,
    SkillSnapshotEntry,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolInvocationContext


# LLM: 所有宿主与workspace状态归入pytest私有根，后端用echo不连接模型。
# 函数用途: 建立含真实工具注册表的独立代理实例。
@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        execution_mode="local_unmanaged"), tmp_path / "workspace")


# LLM: 明确允许工具面，绕过的仅外部调度；调用真实execute_scoped，不执行任何业务工具。
# 函数用途: 读取本用例当前授权范围内的自我目录JSON。
def _read(agent, params, allowed):
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    result = agent.tools.tools["self_map"].execute_scoped(params, ToolInvocationContext(snapshot))
    assert result.ok, result.output
    value = json.loads(result.output)
    assert "capability_families" in value
    return value


# LLM: 测试只冻结公开元数据，path不落文件，任何意外正文读取都会失败。
# 函数用途: 建立能够明确核对类别和停用状态的独立方法快照。
def _skills(agent, *, owner=None, errors=()):
    entries = tuple(SkillSnapshotEntry("owner:" + str(i), str(i), "用途", "/nonexistent/SKILL.md",
        "owner", category, "a" * 64, enabled=enabled)
        for i, (category, enabled) in enumerate((("security", True), ("security", True), ("future_family", True), ("hidden_family", False))))
    return SkillSnapshot(entries, errors, "test-frozen", owner or agent.home_paths.owner_id, str(agent.tools.workspace_root))


# LLM: 运行时改变类别并缩小授权范围，类别必须动态变化且不受分页条数影响。
# 函数用途: 核对工具能力族来自当前公开快照而非静态全表。
def test_dynamic_tool_families_match_scoped_snapshot_and_count_whole_page(agent):
    tool = agent.tools.tools["read_file"]
    tool.model_spec = replace(tool.model_spec, hints=replace(tool.model_spec.hints, category="future_native"))
    allowed = ["self_map", "read_file"]
    value = _read(agent, {"section": "tools", "limit": 1}, allowed)
    assert value["capability_families"] == {"future_native": 1, "introspection": 1}
    assert value["returned"] == 1
    narrowed = _read(agent, {"section": "tools"}, ["self_map"])
    assert narrowed["capability_families"] == {"introspection": 1}
    assert "filesystem" not in json.dumps(narrowed)


# LLM: 目录读取不得回服务重载；停用项不计数，总览与分节须使用同口径。
# 函数用途: 核对无参总览与分页返回的动态工具/方法类别。
def test_overview_contains_dynamic_tool_and_skill_families(agent, monkeypatch):
    agent._current_skill_snapshot = _skills(agent)
    monkeypatch.setattr(agent.skills_service, "snapshot_for", lambda *a, **k: pytest.fail("reload"))
    value = _read(agent, {}, ["self_map", "read_file"])
    assert value["capability_families"]["tools"] == {"filesystem": 1, "introspection": 1}
    assert value["capability_families"]["skills"] == {"future_family": 1, "security": 2}
    page = _read(agent, {"section": "skills", "limit": 1}, ["self_map"])
    assert page["capability_families"] == value["capability_families"]["skills"]
    assert page["returned"] == 1 and "hidden_family" not in json.dumps(page)


# LLM: 未绑定、跨Owner和不完整快照都不能把未知报为0或泄露外Owner类别。
# 函数用途: 核对故障/隔离状态下的类别与总数完整性。
@pytest.mark.parametrize("condition", ["missing", "foreign", "incomplete"])
def test_unknown_skill_inventory_does_not_become_empty_or_leak_categories(agent, condition):
    if condition != "missing":
        errors = (SkillLoadError("private", "SOURCE_MISSING", "private"),) if condition == "incomplete" else ()
        agent._current_skill_snapshot = _skills(agent, owner="other" if condition == "foreign" else None, errors=errors)
    value = _read(agent, {"section": "skills"}, ["self_map"])
    assert value["capability_families"] is None and value["total"] is None
    overview = _read(agent, {}, ["self_map"])
    assert overview["capability_families"]["skills"] is None
    if condition == "foreign":
        assert "security" not in json.dumps(value) and "future_family" not in json.dumps(value)


# LLM: 使用真实restricted快照，不能因展示目录恢复父代理可见全集。
# 函数用途: 验证子集方法类别统计不越出明确授权范围。
def test_child_skill_subset_not_parent_catalog(agent):
    snapshot = _skills(agent)
    agent._current_skill_snapshot = snapshot.restricted(["owner:2"])
    value = _read(agent, {"section": "skills"}, ["self_map"])
    assert value["capability_families"] == {"future_family": 1}


# LLM: FF-08反复使用同一ToolInvocationContext，live registry变化不能改写冻结库存。
# 函数用途: 核对启停、移除、元数据和默认隐藏变化不影响快照的状态、类别与总数。
@pytest.mark.parametrize('allowed', [None, ['self_map', 'read_file']])
@pytest.mark.parametrize('mutation', ['enable', 'disable', 'remove', 'metadata', 'hidden'])
def test_self_map_tool_inventory_is_frozen_after_registry_mutation(agent, allowed, mutation):
    registry = agent.tools
    if mutation == 'enable':
        registry.disabled_tool_names = registry.disabled_tool_names | {'read_file'}
    snapshot = registry.runtime_snapshot(allowed_tools=allowed)
    invocation = ToolInvocationContext(snapshot)
    tool = registry.tools['self_map']
    before_result = tool.execute_scoped({'section': 'tools', 'limit': 100}, invocation)
    assert before_result.ok, before_result.output
    before = json.loads(before_result.output)
    if mutation == 'enable':
        registry.disabled_tool_names = registry.disabled_tool_names - {'read_file'}
    elif mutation == 'disable':
        registry.disabled_tool_names = registry.disabled_tool_names | {'read_file'}
    elif mutation == 'remove':
        registry.tools.pop('read_file')
    elif mutation == 'metadata':
        original = registry.tools['read_file'].model_spec
        registry.tools['read_file'].model_spec = replace(original, description='changed live',
            hints=replace(original.hints, category='drifted_live'))
    else:
        registry.default_hidden_tool_names = registry.default_hidden_tool_names | {'read_file'}
    after_result = tool.execute_scoped({'section': 'tools', 'limit': 100}, invocation)
    assert after_result.ok, after_result.output
    after = json.loads(after_result.output)
    assert after['total'] == before['total']
    assert after['capability_families'] == before['capability_families']
    assert after['counts'] == before['counts']
    assert after['items'] == before['items']


# LLM: FF-08审查补测固定同一快照及加载事实，只变更live分类和默认折叠开关。
# 函数用途: 验证展示分类、展开字段及直接/收起计数也严格沿冻结声明。
@pytest.mark.parametrize('surface', ['direct', 'deferred', 'loaded', 'preloaded'])
def test_self_map_display_ignores_live_classification(agent, surface):
    registry = agent.tools
    registry.catalog_default_deferral_enabled = True
    snapshot = registry.runtime_snapshot(allowed_tools=['self_map', 'tool_search', 'read_file', 'workflows'])
    if surface != 'direct':
        snapshot = replace(snapshot, presentation_deferred_names=frozenset({'read_file'}))
    if surface == 'loaded':
        snapshot = replace(snapshot, loaded_tool_names=('read_file',))
    if surface == 'preloaded':
        snapshot = replace(snapshot, preloaded_tool_names=('read_file',))
    context = ToolInvocationContext(snapshot)
    tool = registry.tools['self_map']
    result = tool.execute_scoped({'section': 'tools', 'limit': 100}, context)
    assert result.ok, result.output
    before = json.loads(result.output)
    registry.catalog_deferred_categories = [*registry.catalog_deferred_categories, 'filesystem']
    registry.catalog_default_deferral_enabled = False
    result = tool.execute_scoped({'section': 'tools', 'limit': 100}, context)
    assert result.ok, result.output
    after = json.loads(result.output)
    assert after['items'] == before['items']
    assert after['counts'] == before['counts']
    rows = {row['id']: row for row in after['items']}
    assert rows['read_file']['state'] == ('deferred' if surface == 'deferred' else 'ready')
    assert rows['workflows']['state'] == 'deferred'
