"""原 Skill 授权不变的单次 prompt 展示减量；不调用模型，不把建议当正文加载。"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig


# LLM: 真实 SkillsService 负责索引及权限，正文标记只用于证明 build 没有提前读取正文。
# 函数用途: 创建固定可发现技能和原 builder，测试真实字符串及缓存布局。
def setup_surface(tmp_path, factory, count=60, *, window=200_000):
    root = tmp_path / "skills"
    for index in range(count):
        path = root / f"method-{index:03d}" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: method-{index:03d}\ndescription: 处理任务编号{index:03d}的独立方法、来源核对和验收步骤\n---\nBODY-ONLY-{index:03d}\n", encoding="utf-8")
    catalog = factory(tmp_path / "home", extra_roots=[root])
    builder = PromptBuilder(AgentConfig(prompt_files=[], model_context_window_tokens=window), tmp_path)
    builder.capability_router = catalog.router
    return catalog, builder


# LLM: 选择只在新 ToolSections 值内传递，固定工作区文本避免用时间变化掩盖前缀比较。
# 函数用途: 沿原 PromptBuilder 接口构造一个真实 native 模型输入。
def build(builder, selected=None, required=(), *, request=False, scope="default"):
    sections = ToolSections(native_tool_use=True, tool_catalog_section="# Tools\n原工具目录",
                            tool_recommendations_section="# Recommended Tools\n原工具推荐",
                            selected_skill_ids=selected, required_skill_ids=required)
    if request:
        return builder.build(request=PromptBuildRequest("核对来源", [], tools=sections, context_scope=scope, workspace_context_override="固定工作区"))
    return builder.build("核对来源", [], tools=sections, context_scope=scope, workspace_context_override="固定工作区")


def test_none_retains_original_layout_and_does_not_consume_required_projection(tmp_path, skill_catalog_factory):
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    original = builder.build("核对来源", [], tools=ToolSections(
        native_tool_use=True, tool_catalog_section="# Tools\n原工具目录", tool_recommendations_section="# Recommended Tools\n原工具推荐"),
        workspace_context_override="固定工作区")
    for actual in (build(builder), build(builder, required=("workspace:method-001",), request=True)):
        assert str(actual) == str(original)
        assert prompt_cache_layout(actual) == prompt_cache_layout(original)
    legacy_index = catalog.router.render_skill_metadata_index(context_window_tokens=200_000)
    assert legacy_index == catalog.router.render_skill_metadata_index(context_window_tokens=200_000, selected_skill_ids=None)
    assert legacy_index in prompt_cache_layout(original).stable_prefix


def test_selection_reduces_real_prompt_and_keeps_names_only_in_original_volatile_section(tmp_path, skill_catalog_factory):
    _, builder = setup_surface(tmp_path, skill_catalog_factory)
    original = build(builder)
    chosen = build(builder, ("workspace:method-001", "workspace:method-007"), request=True)
    layout = prompt_cache_layout(chosen)
    assert len(chosen.encode()) < len(original.encode()) * 0.6
    assert "Skill Discovery" in layout.stable_prefix and "method-" not in layout.stable_prefix
    recommended = dict(layout.volatile_sections)["prompt.tool_recommendations"]
    assert "原工具推荐" in recommended and "workspace:method-001" in recommended and "workspace:method-007" in recommended
    assert "method-002" not in chosen and "58 个授权 Skill 未展示" in chosen
    assert "BODY-ONLY" not in chosen
    other = build(builder, ("workspace:method-002",))
    assert prompt_cache_layout(other).stable_prefix == layout.stable_prefix
    assert prompt_cache_layout(other).volatile_sections != layout.volatile_sections


def test_omitted_skill_is_still_searchable_and_get_returns_original_body(tmp_path, skill_catalog_factory):
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory)
    before = catalog.snapshot
    projected = build(builder, ("workspace:method-001",))
    assert "method-059" not in projected
    tool = SkillSearchTool(SimpleNamespace(capability_router=catalog.router, current_skill_snapshot=lambda: catalog.snapshot))
    search = tool.execute({"action": "search", "query": "method-059", "limit": 1})
    assert search.ok and "workspace:method-059" in search.output
    body = tool.execute({"action": "get", "skill_id": "workspace:method-059"})
    assert body.ok and "BODY-ONLY-059" in body.output
    assert catalog.snapshot is before and len(before.entries) == 60


def test_build_never_calls_snapshot_read_body(tmp_path, skill_catalog_factory, monkeypatch):
    _, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    def forbidden(*args, **kwargs):
        raise AssertionError("展示名卡不得读取正文")
    monkeypatch.setattr(SkillSnapshot, "read_body", forbidden)
    result = build(builder, ("workspace:method-001",), ("workspace:method-002",))
    assert "workspace:method-001" in result and "workspace:method-002" in result


def test_required_authorized_ids_survive_tiny_budget_unknown_and_name_alias_do_not(tmp_path, skill_catalog_factory):
    _, builder = setup_surface(tmp_path, skill_catalog_factory, 3, window=1)
    result = build(builder, ("workspace:method-000", "method-001", "owner:missing"),
                   ("workspace:method-002", "workspace:method-001", "other-owner:secret"))
    assert "workspace:method-001" in result and "workspace:method-002" in result
    assert "method-000" not in result and "other-owner:secret" not in result and "owner:missing" not in result


def test_empty_selection_and_unknown_ids_leave_only_fixed_discovery(tmp_path, skill_catalog_factory):
    _, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    empty = build(builder, ())
    unknown = build(builder, ("workspace:gone", "method-001"))
    assert empty == unknown and "method-" not in empty
    assert "skill_search(action=search)" in empty and "action=get" in empty


def test_current_scoped_snapshot_removal_prevents_stale_selection_and_required_ids(tmp_path, skill_catalog_factory):
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    current = catalog.snapshot.restricted(("workspace:method-001",))
    router = CapabilityRouter(skill_snapshot_provider=lambda: current)
    builder.capability_router = router
    result = build(builder, ("workspace:method-000", "workspace:method-001"), ("workspace:method-002",))
    assert "workspace:method-001" in result and "method-000" not in result and "method-002" not in result
    current = catalog.snapshot.restricted(())
    assert "method-001" not in build(builder, ("workspace:method-001",), ("workspace:method-001",))


def test_shared_builder_concurrent_requests_do_not_share_selected_ids(tmp_path, skill_catalog_factory):
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory, 8)
    original = build(builder)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda index: build(builder, (f"workspace:method-{index:03d}",)), range(8)))
    for index, result in enumerate(results):
        assert f"workspace:method-{index:03d}" in result
        assert all(f"workspace:method-{other:03d}" not in result for other in range(8) if other != index)
    assert build(builder) == original and len(catalog.snapshot.entries) == 8


def test_task_local_projection_only_renders_real_restricted_snapshot_and_preserves_stable_scope(tmp_path, skill_catalog_factory, monkeypatch):
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory, 4)
    restricted = catalog.snapshot.restricted(("workspace:method-001", "workspace:method-002"))
    builder.capability_router = CapabilityRouter(skill_snapshot_provider=lambda: restricted)
    baseline = build(builder, scope="task_local")
    def forbidden(*args, **kwargs):
        raise AssertionError("展示不能预读正文")
    monkeypatch.setattr(SkillSnapshot, "read_body", forbidden)
    result = build(builder, ("workspace:method-000", "workspace:method-001"),
                   ("workspace:method-002", "workspace:method-003"), scope="task_local")
    assert "workspace:method-001" in result and "workspace:method-002" in result
    assert "method-000" not in result and "method-003" not in result and "BODY-ONLY" not in result
    assert prompt_cache_layout(result).stable_prefix == prompt_cache_layout(baseline).stable_prefix
    assert "method-" not in baseline and "Skill Discovery" not in result
    assert "workspace:method-002" in dict(prompt_cache_layout(result).volatile_sections)["prompt.tool_recommendations"]


def test_isolated_and_control_plane_do_not_gain_projection_content(tmp_path, skill_catalog_factory):
    _, builder = setup_surface(tmp_path, skill_catalog_factory, 3)
    for scope in ("isolated", "control_plane"):
        baseline = build(builder, scope=scope)
        selected = build(builder, ("workspace:method-001",), ("workspace:method-002",), scope=scope)
        assert selected == baseline and "Selected Skills" not in selected
