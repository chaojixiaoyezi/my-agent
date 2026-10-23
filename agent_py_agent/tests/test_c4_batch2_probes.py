# LLM: 所有I/O只在pytest临时根；检查真实预算/序列化入口，不调用外部模型或改动Owner原件。
# 模块用途: 锁住冻结审查的Owner/工作区信封、人格预算、参数顺序与目录单一限额入口。
from copy import deepcopy

import pytest

from agent_py_agent.agent.backends.openai_chat import _openai_tools_from_native
from agent_py_agent.agent.capability import router as catalog
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.prompting_parts import builder
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_persona_repository import _repository

OWNER_MARKER = "\n[... owner scope truncated by 6000-char envelope budget ...]\n"
WS_MARKER = "\n[... workspace context truncated by 6000-char envelope budget ...]"
PERSONA_MARKER = "\n[... persona content truncated by configured prompt budget ...]\n"
HEADER = "# Owner Scope\n\n\n# Workspace Context\n\n\n"
FACTS = "- 当前工具工作目录: /job\n- current_local_date: 2026-10-23\n"


# LLM: 两段都超额时既要保留Owner首尾边界，也要给真实cwd/日期留预算。
# 函数用途: 验证联合信封完整长度、两种标记及完整行裁剪。
def test_owner_workspace_envelope_keeps_edges_facts_and_markers():
    owner = "- OWNER_OPEN\n" + "- owner rule\n" * 800 + "- OWNER_CLOSE\n"
    workspace = FACTS + "- workspace detail\n" * 800
    o, w = builder._bounded_owner_workspace_sections(owner, workspace)
    assert len(HEADER) + len(o) + len(w) <= 6000
    assert o.startswith("- OWNER_OPEN\n") and o.endswith("- OWNER_CLOSE\n")
    assert OWNER_MARKER in o
    assert w.startswith(FACTS) and w.endswith(WS_MARKER)
    for part in o.split(OWNER_MARKER):
        assert all(line in owner.splitlines(keepends=True) for line in part.splitlines(keepends=True))
    assert all(line in workspace.splitlines(keepends=True) for line in w.removesuffix(WS_MARKER).splitlines(keepends=True))


# LLM: 单行Owner也必须保留尾部和标记；短workspace不能因Owner优先占满而被吞掉。
# 函数用途: 原样覆盖p3_budgets.py Part B的输入与观察点。
def test_owner_workspace_probe_single_line_owner_preserves_short_workspace():
    owner = "O" * 5990 + "【归属边界结尾】"
    o, w = builder._bounded_owner_workspace_sections(owner, FACTS)
    assert o.endswith("【归属边界结尾】") and OWNER_MARKER in o
    assert w == FACTS
    assert len(HEADER) + len(o) + len(w) <= 6000


# LLM: 不超预算必须逐字保持；超额workspace使用Owner没用掉的余量而非固定压成1500字符。
# 函数用途: 覆盖空段、精确边界和Workspace单独超额。
@pytest.mark.parametrize("owner,workspace", [("", ""), ("owner", FACTS), ("x" * (6000 - len(HEADER)), "")])
def test_owner_workspace_envelope_preserves_in_budget_bytes(owner, workspace):
    assert builder._bounded_owner_workspace_sections(owner, workspace) == (owner, workspace)


# LLM: 在用Owner很短时把剩余预算全交给workspace，保留领先事实和截断提示。
# 函数用途: 检查workspace分配不是僵硬的最小保留额。
def test_workspace_can_use_owner_unused_budget():
    workspace = FACTS + "- detail\n" * 1200
    o, w = builder._bounded_owner_workspace_sections("owner", workspace)
    assert o == "owner" and w.startswith(FACTS) and w.endswith(WS_MARKER)
    assert len(w) > 5000 and len(HEADER) + len(o) + len(w) <= 6000


# LLM: 默认20000是普通预算值，不是AGENTS特殊无限哨兵；只读加载不得重写原件。
# 函数用途: 对三个真实人格目标核对默认预算、原标记、首尾规则与诊断。
@pytest.mark.parametrize("target", ["agents", "soul", "user"])
def test_all_persona_targets_obey_default_prompt_budget(tmp_path, target):
    repo = _repository(tmp_path / "owner")
    text = f"# {target.upper()}\n" + "- normal rule\n" * 2200 + "- KEEP_LAST\n"
    path = repo.path_for(target)
    path.write_bytes(text.encode())
    snapshot = repo.load(target)
    assert snapshot.diagnostic.truncated is True
    assert snapshot.diagnostic.state == "truncated"
    assert len(snapshot.content) <= 20_000 and PERSONA_MARKER in snapshot.content
    assert snapshot.content.startswith(f"# {target.upper()}\n") and snapshot.content.endswith("- KEEP_LAST\n")
    assert path.read_bytes() == text.encode()


# LLM: 显式更高预算仍受用户配置控制，去掉默认无限哨兵不能变成强制20000硬编码。
# 函数用途: 验证长AGENTS在足够的显式预算下保持完整。
def test_explicit_larger_agents_budget_keeps_complete_document(tmp_path):
    text = "# AGENTS\n" + "- normal rule\n" * 2200
    repo = _repository(tmp_path / "owner", prompt_max_chars=len(text) + 1)
    repo.path_for("agents").write_text(text)
    snapshot = repo.load("agents")
    assert snapshot.content == text and snapshot.diagnostic.truncated is False


# LLM: 使用真实来源仓库的默认本地读链，不靠普通文件入口的通过推断来源入口也有界。
# 函数用途: 验证绑定来源的AGENTS同样执行默认20000预算。
def test_bound_default_agents_source_obeys_default_prompt_budget(tmp_path):
    root = tmp_path / "data"
    repo = _repository(root / "owners/local/main")
    text = "# AGENTS\n" + "- normal rule\n" * 2200 + "- KEEP_LAST\n"
    repo.path_for("agents").write_text(text)
    repo.agents_source = AgentsSourceRepository(owner="local/main", owner_home=repo.owner_home,
        path_policy=PathAccessPolicy.from_values(mode="full_access", agent_home_root=root))
    snapshot = repo.load("agents")
    assert snapshot.diagnostic.truncated is True
    assert len(snapshot.content) <= 20_000 and PERSONA_MARKER in snapshot.content
    assert snapshot.content.endswith("- KEEP_LAST\n") and repo.path_for("agents").read_text() == text


# LLM: 直接检查供应商工具投影，既保留properties参数顺序又稳定schema定义键，不修改输入。
# 函数用途: 覆盖p3 Part E并核对嵌套参数与数组顺序。
def test_chat_schema_preserves_properties_order_but_sorts_definitions():
    schema = {"type": "object", "required": ["path", "content"], "additionalProperties": False,
        "properties": {"path": {"type": "string", "description": "target"},
            "mode": {"oneOf": [{"type": "object", "properties": {"z": {"type": "string"}, "a": {"type": "string"}}}]},
            "content": {"type": "string"}}}
    before = deepcopy(schema)
    params = _openai_tools_from_native([{"name": "write_file", "input_schema": schema}])[0]["function"]["parameters"]
    assert list(params["properties"]) == ["path", "mode", "content"]
    assert list(params) == sorted(schema)
    assert list(params["properties"]["path"]) == ["description", "type"]
    assert list(params["properties"]["mode"]["oneOf"][0]["properties"]) == ["z", "a"]
    assert params["required"] == ["path", "content"] and schema == before


# LLM: 参数本身可合法叫properties，名称不能误把它的schema定义当成参数集合。
# 函数用途: 防止基于父键的排序例外污染同名参数的定义键。
def test_chat_schema_property_named_properties_still_sorts_its_definition():
    schema = {"properties": {"properties": {"type": "string", "description": "value"}, "aaa": {"type": "string"}}, "type": "object"}
    params = _openai_tools_from_native([{"name": "custom", "input_schema": schema}])[0]["function"]["parameters"]
    assert list(params["properties"]) == ["properties", "aaa"]
    assert list(params["properties"]["properties"]) == ["description", "type"]


# LLM: 命名schema集合允许定义名为properties；名称不等于schema关键字，递归须进入定义节点。
# 函数用途: 复现独立审查指出的命名定义误判，并覆盖同构的标准schema映射。
@pytest.mark.parametrize("keyword", ["$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"])
def test_chat_schema_named_properties_definition_keeps_nested_parameters(keyword):
    schema = {keyword: {"properties": {"type": "object", "description": "named definition",
        "properties": {"path": {"type": "string"}, "mode": {"type": "string"}, "content": {"type": "string"}}}}}
    params = _openai_tools_from_native([{"name": "named", "input_schema": schema}])[0]["function"]["parameters"]
    assert list(params[keyword]["properties"]) == ["description", "properties", "type"]
    assert list(params[keyword]["properties"]["properties"]) == ["path", "mode", "content"]
    assert params == schema


# LLM: 同时检查死入口移除与真实新读口，不能只靠名字不存在宣称行为正确。
# 函数用途: 保护删除未使用包装器后的提示读取及消重。
def test_removed_prompt_reader_wrapper_keeps_real_reading(tmp_path):
    assert not hasattr(builder, "_read_prompt_file")
    (tmp_path / "rule.md").write_text("RULE")
    reader = builder.PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    assert len(reader.read_prompt_files(["rule.md", "rule.md"])) == 1
    assert reader.read_prompt_files(["rule.md"])[0].endswith("RULE")


# LLM: 计数中央限额入口的调用，并用真实目录渲染验证字符上限、遗漏提示和说明完整性。
# 函数用途: 确保公开/选择目录及混合能力包共用一次12k信封限额。
@pytest.mark.parametrize("selected,mixed", [(False, False), (True, False), (False, True), (True, True)])
def test_skill_catalog_uses_one_envelope_enforcer(monkeypatch, selected, mixed):
    original = getattr(catalog, "_enforce_skill_catalog_envelope", None)
    seen = []

    # LLM: 只观察中央帮助函数调用；真实渲染与预算计算不替换。
    # 函数用途: 给重构前后留下可断言的唯一限额接缝。
    def observe(text, *args, **kwargs):
        seen.append(1)
        return original(text, *args, **kwargs) if original is not None else text

    monkeypatch.setattr(catalog, "_enforce_skill_catalog_envelope", observe, raising=False)
    available = [catalog.CapabilityCard(id=f"skill:{i}", kind="skill", name=f"skill-{i:03d}",
        description="description " * 100, metadata={"stable_id": f"owner:skill-{i:03d}"}) for i in range(400)]
    if mixed:
        available += [catalog.CapabilityCard(id=f"package:{i}", kind="capability_package", name=f"package-{i:03d}",
            description="description " * 100, metadata={"package_id": f"package-{i:03d}", "stable_id": f"capability:package-{i:03d}"}) for i in range(100)]
    router = catalog.CapabilityRouter(extra_cards=available)
    text = router.render_skill_metadata_index(context_window_tokens=1_000_000,
        selected_skill_ids=tuple(c.metadata["stable_id"] for c in available) if selected else None)
    assert seen == [1]
    assert len(text) <= 12_000 and "skill_search" in text
    assert "未显示" in text or "未展示" in text
    if not selected:
        assert catalog._SKILL_USAGE_INSTRUCTIONS in text
