# LLM: 使用真实 owner 注册表验证发现、Schema 展示和归档合同；不调用外部服务或扩大工具权限。
# 模块用途: 锁住紧凑工具回执、批量加载、弱信号与追加顺序。
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    KeywordToolSearchProvider,
    ToolModelHints,
    ToolSearchHit,
    _score_keyword_token,
)
from agent_py_agent.agent.tooling.registry import (
    ToolSearchTool,
    _render_recommended_tools,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_model_spec


# LLM: 每例独立临时 owner，使用产品装配但不发模型或网络请求。
# 函数用途: 准备默认启用工具折叠的真实注册表。
@pytest.fixture
def registry(tmp_path):
    config = AgentConfig(enable_tools=True, enable_subagents=True,
                         memory_path=str(tmp_path / "memory.jsonl"),
                         tool_default_deferral_enabled=True)
    return SimpleAgent(config, str(tmp_path)).tools


# LLM: Schema 与运行期校验必须一致；空批量或类型错误不能偷偷加载工具。
# 函数用途: 验证精确 names 与查询的参数合同。
@pytest.mark.parametrize("params", [{}, {"query": " "}, {"names": []}, {"names": "scripts"},
                                    {"names": [1]}, {"names": [""]}, {"names": ["scripts"] * 17},
                                    {"query": "scripts", "limit": 0},
                                    {"query": "scripts", "limit": True}])
def test_search_rejects_invalid_discovery_arguments(registry, params):
    result = registry.tools["tool_search"].execute(params)
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"


# LLM: 只在指定快照内逐项解析名字；重复名保序去重，未知及不可用名不成为加载事实。
# 函数用途: 验证一次批量请求的成功、不可用和未找到分流。
def test_exact_batch_is_ordered_scoped_and_reports_every_failure(registry):
    snapshot = registry.runtime_snapshot(allowed_tools=["tool_search", "scripts", "workflows", "offline_probe"])
    snapshot = replace(snapshot, unavailable_tools=(("offline_probe", "TOOL_UNAVAILABLE", "not connected"),))
    result = registry.tools["tool_search"]._execute_snapshot(
        {"names": ["workflows", "scripts", "workflows", "offline_probe", "manage_models", "unknown"]}, snapshot)
    assert result.ok
    receipt = json.loads(result.output)
    assert result.result_envelope["tool_search"] == {"loaded_tool_names": ["workflows", "scripts"]}
    assert receipt["not_found"] == ["manage_models", "unknown"]
    assert receipt["unavailable"][0]["error_code"] == "TOOL_UNAVAILABLE"
    assert receipt["unavailable"][0]["reason"] == "not connected"


# LLM: 原 v3 全定义留档，v4 live 只证明加载；两者 Schema 哈希与 typed 信封一致。
# 函数用途: 防止搜索回执再次复制 input_schema 或丢掉跨 Compact 加载事实。
def test_native_receipt_keeps_full_archive_but_no_live_schema(registry):
    result = registry.tools["tool_search"].execute({"names": ["scripts", "workflows"]})
    assert result.ok
    archive = json.loads(result.output)
    live = json.loads(result.result_envelope["tool_output_policy"]["live_prompt_output"])
    assert archive["schema_version"] == 3 and archive["tools"][0]["input_schema"]
    assert live["schema_name"] == "tool_search_output" and live["schema_version"] == 4
    assert "input_schema" not in json.dumps(live)
    assert live["names"] == ["scripts", "workflows"]
    assert live["loaded_for_next_model_call"] == ["scripts", "workflows"]
    assert live["loading_scope"] == "current_thread"
    assert live["persists_across_compact"] and live["authorization_rechecked_each_run"]
    assert [item["schema_hash"] for item in live["tools"]] == [item["schema_hash"] for item in archive["tools"]]
    assert all(item["status"] == "loaded" for item in live["tools"])
    assert result.result_envelope["tool_output_policy"]["preserve_prompt_output"]


# LLM: 精确名字与 query 可共同使用，默认 query 只加载三项，不截断显式 names。
# 函数用途: 验证参数 Schema 和两种发现方式的合并。
def test_names_schema_and_combined_exact_query(registry):
    schema = ToolSearchTool.model_spec.input_schema
    assert schema["properties"]["names"]["maxItems"] == 16
    assert schema.get("required") != ["query"]
    assert ToolSearchTool._DEFAULT_SEARCH_LIMIT == 3
    result = registry.tools["tool_search"].execute({"names": ["scripts"], "query": "workflows"})
    assert result.ok
    assert result.result_envelope["tool_search"]["loaded_tool_names"] == ["scripts", "workflows"]


# LLM: 低分候选有可见名卡但不成为真正加载；不依赖模型是否选择工具。
# 函数用途: 复现只有用途描述命中的弱信号不得自动装载。
def test_weak_description_match_is_candidate_not_loaded(registry):
    spec = make_test_model_spec("weak_probe", description="用于琥珀能力", category="testing")
    spec = replace(spec, hints=ToolModelHints(category="testing", default_deferred=True, deferred_summary="用途"))
    tool = type("Probe", (BaseTool,), {"model_spec": spec, "runtime_policy": ToolSearchTool.runtime_policy})()
    registry.register(tool)
    result = registry.tools["tool_search"].execute({"query": "琥珀"})
    live = json.loads(result.result_envelope["tool_output_policy"]["live_prompt_output"])
    assert "weak_probe" not in result.result_envelope["tool_search"]["loaded_tool_names"]
    assert any(item["name"] == "weak_probe" for item in live["candidates"])


# LLM: 通用中文二字只在名字、类别或显式 keywords 命中时得分，非通用二字描述权重降低。
# 函数用途: 固定中文弱信号计分，保留显式关键词召回。
def test_chinese_stopwords_do_not_score_prose_but_keywords_still_score():
    haystacks = {"name": "probe", "category": "general", "keywords": "", "description": "你好回复收到创建琥珀", "use_cases": ""}
    assert _score_keyword_token("创建", haystacks)[0] == 0
    assert _score_keyword_token("琥珀", haystacks)[0] == 0.25
    haystacks["keywords"] = "创建"
    assert _score_keyword_token("创建", haystacks)[0] >= 4


# LLM: 使用真实工具元数据检索，不把自然语言请求改写成硬编码路由。
# 函数用途: 验证普通问候和 Skill/Workflow 请求不乱加载管理工具。
@pytest.mark.parametrize("query", ["你好，只回复收到。", "创建一个skill", "创建一个workflow+skill的工作流"])
def test_chinese_requests_do_not_load_unrelated_administration(registry, query):
    result = registry.tools["tool_search"].execute({"query": query})
    loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
    assert not {"restart_gateway", "manage_models"}.intersection(loaded)
    if "skill" in query:
        assert set(loaded).intersection({"skill_search", "workflows", "scripts", "skill_transfer", "skill_summarize"})
    else:
        assert not loaded
    text = registry.render_recommended_tools_section(query)
    if query.startswith("你好"):
        assert text == ""


# LLM: 推荐门槛以 retriever 的分数为准，不因候选存在就生成低置信名卡。
# 函数用途: 验证不足三分的推荐返回空文本。
def test_recommendations_require_minimum_score():
    spec = make_test_model_spec("weak_probe", description="琥珀")
    text = _render_recommended_tools("琥珀", [spec], retriever=KeywordToolSearchProvider(),
                                    retrieval_limit=3, detail_max_chars=0, tool_protocol="native")
    assert text == ""
    assert ToolSearchHit("weak_probe", 2.99, []).score < 3


# LLM: 发现工具只能追加至稳定基础表尾部；每轮仍取当前授权和就绪交集。
# 函数用途: 验证注册位置不影响新发现 Schema 的追加顺序。
def test_discovered_schemas_append_in_discovery_order_and_recheck_permissions(registry):
    snapshot = registry.runtime_snapshot()
    assert hasattr(snapshot, "loaded_tool_names")
    base = registry.specs_for_snapshot(snapshot)
    names = [spec.name for spec in base]
    assert "package_build" not in names and "package_install" not in names
    first = replace(snapshot, loaded_tool_names=("workflows", "scripts"))
    second = replace(snapshot, loaded_tool_names=("workflows", "scripts", "package_build"))
    first_names = [spec.name for spec in registry.specs_for_snapshot(first)]
    assert first_names == [*names, "workflows", "scripts"]
    assert [spec.name for spec in registry.specs_for_snapshot(second)] == [*first_names, "package_build"]
    leaf = registry.runtime_snapshot(allowed_tools=["tool_search", "read_file"])
    leaf = replace(leaf, loaded_tool_names=("workflows", "scripts"))
    assert {spec.name for spec in registry.specs_for_snapshot(leaf)} == {"tool_search", "read_file"}


# LLM: 有搜索入口才折叠；没有搜索入口的受限叶子必须直接拿到全部原授权工具。
# 函数用途: 验证后台 allowlist 与 presentation 相互独立。
def test_allowed_snapshot_can_defer_but_leaf_without_search_stays_direct(registry):
    scoped = registry.runtime_snapshot(allowed_tools=["tool_search", "read_file", "scripts"])
    scoped = replace(scoped, presentation_deferred_names=frozenset({"scripts"}))
    visible = registry.model_visible_specs(runtime_snapshot=scoped, allowed_tools=list(scoped.allowed_tools))
    assert {spec.name for spec in visible} == {"tool_search", "read_file"}
    loaded = registry.model_visible_specs(runtime_snapshot=scoped, loaded_tool_names=("scripts",))
    assert [spec.name for spec in loaded][-1] == "scripts"
    leaf = registry.runtime_snapshot(allowed_tools=["read_file", "scripts"])
    assert {spec.name for spec in registry.model_visible_specs(runtime_snapshot=leaf)} == {"read_file", "scripts"}


# LLM: 调用方再收窄同一快照时，展示与发现入口也必须取实际 Schema 子集，不能藏起唯一授权工具。
# 函数用途: 验证完整快照加窄 allowlist 的叶子仍直接拿到工具。
def test_narrowed_full_snapshot_without_search_does_not_hide_authorized_tools(registry):
    snapshot = registry.runtime_snapshot()
    visible = registry.model_visible_specs(allowed_tools=["scripts"], runtime_snapshot=snapshot)
    assert [spec.name for spec in visible] == ["scripts"]


# LLM: 同一名字既被显式加载又有模糊弱命中时只能有一个最终状态，不重复计数或要求再次加载。
# 函数用途: 验证混合 names/query 不产生重复的待加载候选。
def test_explicit_name_is_not_duplicated_in_weak_candidates(registry):
    spec = make_test_model_spec("weak_probe", description="用于琥珀能力", category="testing")
    spec = replace(spec, hints=ToolModelHints(category="testing", default_deferred=True, deferred_summary="用途"))
    registry.register(type("Probe", (BaseTool,), {"model_spec": spec, "runtime_policy": ToolSearchTool.runtime_policy})())
    result = registry.tools["tool_search"].execute({"query": "琥珀", "names": ["weak_probe"]})
    live = json.loads(result.result_envelope["tool_output_policy"]["live_prompt_output"])
    assert result.result_envelope["tool_search"]["loaded_tool_names"] == ["weak_probe"]
    assert live["matched_count"] == 1
    assert not live["candidates"]
