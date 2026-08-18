from __future__ import annotations

"""工具渐进式披露单测 —— 延迟 category 通过 tool_search 按需加载。

背景:每轮 prompt 原本把全部工具的完整 spec 平铺进主目录(`render_catalog_section`),
一句问候也背着一堆垂直工具 → token 膨胀。本特性把 deferred category 从初始 schema 和主目录
正文移出，只留折叠名单；模型用 tool_search 加载命中工具，list_tools 仍可查看完整注册表。
普通对话大幅瘦身，垂直任务功能不丢，deferred=[] 完全恢复老行为。
"""

import json
import tempfile
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _consume_ephemeral_loaded_tools,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path, **overrides) -> SimpleAgent:
    cfg = AgentConfig(
        enable_tools=True,
        enable_subagents=True,
        memory_path=str(tmp_path / "m.jsonl"),
        **overrides,
    )
    return SimpleAgent(cfg, str(tmp_path))


def test_deferred_collaboration_tools_collapsed_in_main_catalog(tmp_path) -> None:
    """collaboration 工具的完整 spec 不在主目录正文，但折叠行列出其名字。"""
    agent = _agent(tmp_path)
    section = agent.tools.render_catalog_section()
    assert "⊞" in section  # 折叠行存在
    body, fold = section.split("⊞", 1)
    # 垂直工具的完整条目不在主目录正文(瘦身的关键)
    assert "raise_collaboration" not in body
    assert "inspect_collaboration" not in body
    # 折叠名单列出它们，模型可据此发起 tool_search。
    assert "raise_collaboration" in fold
    assert "inspect_collaboration" in fold


def test_native_visible_surface_defers_goal_orchestration_and_collaboration(tmp_path) -> None:
    agent = _agent(tmp_path)
    names = {spec.name for spec in agent.tools.model_visible_specs()}

    assert "tool_search" in names
    assert "remember" in names
    assert "get_goal" not in names
    assert "inspect_agent_tree" not in names
    assert "raise_collaboration" not in names


def test_tool_search_returns_compact_candidates_without_loading(tmp_path) -> None:
    agent = _agent(tmp_path)
    result = agent.tools.tools["tool_search"].execute(
        {"query": "create_subagents 创建并管理子代理", "limit": 4}
    )

    assert result.ok
    loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
    assert loaded == []
    payload = json.loads(result.output)
    assert "create_subagents" in {item["name"] for item in payload["tools"]}
    assert all("parameters" not in item for item in payload["tools"])


def test_tool_search_loads_only_explicit_deferred_names_for_next_turn(tmp_path) -> None:
    agent = _agent(tmp_path)
    result = agent.tools.tools["tool_search"].execute(
        {
            "query": "create_subagents 创建并管理子代理",
            "load_names": ["create_subagents"],
        }
    )

    assert result.ok
    loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
    assert loaded == ["create_subagents"]
    names = {
        spec.name
        for spec in agent.tools.model_visible_specs(loaded_tool_names=set(loaded))
    }
    assert "create_subagents" in names
    assert "get_goal" not in names


def test_tool_search_exact_load_is_bounded_and_does_not_reveal_unavailable_names(tmp_path) -> None:
    agent = _agent(tmp_path)
    result = agent.tools.tools["tool_search"].execute(
        {
            "query": "subagents",
            "load_names": ["create_subagents", "not_a_real_tool"],
        }
    )

    assert result.ok
    payload = json.loads(result.output)
    assert payload["loaded_for_next_model_call"] == ["create_subagents"]
    assert payload["not_loaded"] == ["not_a_real_tool"]
    assert result.result_envelope["tool_search"]["loaded_tool_names"] == [
        "create_subagents"
    ]
    loaded_tool = payload["tools"][0]
    assert loaded_tool["input_schema"]["properties"]
    assert loaded_tool["schema_hash"].startswith("sha256:")
    assert "parameters" not in loaded_tool
    assert "required_parameters" not in loaded_tool


def test_loaded_tool_schema_is_consumed_after_one_successful_model_call() -> None:
    params = SimpleNamespace(loaded_tool_names={"create_subagents"})

    _consume_ephemeral_loaded_tools(
        params,
        SimpleNamespace(runtime_status="completed"),
    )

    assert params.loaded_tool_names == set()


def test_loaded_tool_schema_survives_overflow_or_auxiliary_reply() -> None:
    overflow = SimpleNamespace(loaded_tool_names={"create_subagents"})
    auxiliary = SimpleNamespace(loaded_tool_names={"create_subagents"})

    _consume_ephemeral_loaded_tools(
        overflow,
        SimpleNamespace(runtime_status="context_overflow"),
    )
    _consume_ephemeral_loaded_tools(
        auxiliary,
        SimpleNamespace(runtime_status="completed"),
        tool_surface_was_visible=False,
    )

    assert overflow.loaded_tool_names == {"create_subagents"}
    assert auxiliary.loaded_tool_names == {"create_subagents"}


def test_explicit_allowed_tools_are_structured_direct_exposure(tmp_path) -> None:
    agent = _agent(tmp_path)
    names = {
        spec.name
        for spec in agent.tools.model_visible_specs(
            allowed_tools=["inspect_agent_tree", "get_goal"]
        )
    }
    assert names == {"inspect_agent_tree", "get_goal"}


def test_core_tools_stay_in_main_catalog(tmp_path) -> None:
    """通用核心工具(文件/命令)照常在主目录正文,不受折叠影响。"""
    agent = _agent(tmp_path)
    body = agent.tools.render_catalog_section().split("⊞", 1)[0]
    assert "read_file" in body
    assert "run_command" in body


def test_deferred_tools_surface_via_tool_search_not_initial_recommendations(tmp_path) -> None:
    """推荐区不虚报未加载工具；协作任务会推荐 tool_search 作为发现入口。"""
    agent = _agent(tmp_path)
    rec = agent.tools.render_recommended_tools_section("发起跨代理协作 协作请求 collaboration 协助")
    assert "tool_search" in rec
    assert "raise_collaboration" not in rec


def test_native_recommendations_do_not_duplicate_structured_tool_schemas(tmp_path) -> None:
    agent = _agent(tmp_path)

    text = agent.tools.render_recommended_tools_section(
        "查看项目文件并运行测试",
        tool_protocol="native",
    )

    assert "# Recommended Tools" in text
    assert "list_files" in text or "run_command" in text
    assert "参数：" not in text
    assert "属性：effect=" not in text
    assert len(text) < 1_200


def test_normal_query_recommended_stays_small(tmp_path) -> None:
    """普通问候:推荐区不凑不相关的 collaboration 工具(retriever score>0 过滤),prompt 维持精简。"""
    agent = _agent(tmp_path)
    rec = agent.tools.render_recommended_tools_section("请只回复四个字")
    assert rec.count("collaboration") == 0


def test_list_tools_still_lists_all_including_deferred(tmp_path) -> None:
    """list_tools 始终列全部工具(含被折叠的),作为兜底发现入口。"""
    agent = _agent(tmp_path)
    result = agent.tools.tools["list_tools"].execute({})
    assert result.ok
    assert "raise_collaboration" in result.output
    assert "inspect_collaboration" in result.output


def test_empty_deferred_restores_full_catalog(tmp_path) -> None:
    """deferred=[] 完全恢复老行为:全部工具铺主目录,无折叠行(向后兼容)。"""
    agent = _agent(tmp_path, tool_catalog_deferred_categories=[])
    section = agent.tools.render_catalog_section()
    assert "raise_collaboration" in section  # 垂直工具回到主目录正文
    assert "⊞" not in section  # 没有折叠行


def test_deferred_shrinks_catalog_token_footprint(tmp_path) -> None:
    """量化:开启 deferred 后主目录小于全量(瘦身真实发生,不是只挪位置)。"""
    folded = _agent(tmp_path).tools.render_catalog_section()
    full = _agent(tmp_path, tool_catalog_deferred_categories=[]).tools.render_catalog_section()
    assert len(folded) < len(full)


def test_native_catalog_does_not_duplicate_provider_schemas(tmp_path) -> None:
    agent = _agent(tmp_path)
    text = agent.tools.render_catalog_section(tool_protocol="native")
    native = agent.tools.render_catalog_section(tool_protocol="native")

    assert "结构化 Schema 为准" in native
    assert "⊞" in native
    assert "raise_collaboration" in native
    assert len(native) < len(text) // 2
