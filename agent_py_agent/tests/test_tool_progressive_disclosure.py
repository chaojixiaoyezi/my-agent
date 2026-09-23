from __future__ import annotations

"""工具渐进式披露单测 —— 延迟 category 通过 tool_search 按需加载。

背景:每轮 prompt 原本把全部工具的完整 spec 平铺进主目录(`render_catalog_section`),
一句问候也背着一堆垂直工具 → token 膨胀。本特性把 deferred category 从初始 schema 和主目录
正文移出，只留折叠名单；模型用单步 tool_search 搜索并展开命中工具，list_tools 仍可查看完整注册表。
普通对话大幅瘦身，垂直任务功能不丢，deferred=[] 完全恢复老行为。
"""

import json
import tempfile

from agent_py_agent.agent.agent_core.tool_loop.model_turn import request_model_response
from agent_py_agent.agent.backends import ModelResponse
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


def test_explicitly_deferred_goal_tools_collapsed_in_main_catalog(tmp_path) -> None:
    """用户显式选择折叠 Goal 时仍尊重配置，默认工具可见性由另一测试覆盖。"""
    agent = _agent(tmp_path, tool_catalog_deferred_categories=["goal"])
    section = agent.tools.render_catalog_section()
    assert "⊞" in section  # 折叠行存在
    body, fold = section.split("⊞", 1)
    # 垂直工具的完整条目不在主目录正文(瘦身的关键)
    assert "get_goal" not in body
    assert "create_goal" not in body
    # 折叠名单列出它们，模型可据此发起 tool_search。
    assert "get_goal" in fold
    assert "create_goal" in fold


def test_native_visible_surface_keeps_recursive_agent_control_direct(tmp_path) -> None:
    agent = _agent(tmp_path)
    names = {spec.name for spec in agent.tools.model_visible_specs()}

    assert "tool_search" in names
    assert "remember" in names
    assert "create_subagents" in names
    assert "list_agents" in names
    assert "send_guidance" in names
    assert "cancel_subagents" in names
    assert "resolve_capability_requests" in names
    assert "get_goal" in names
    assert "inspect_agent_tree" not in names
    assert "create_goal" in names
    assert "update_goal" in names


def test_tool_search_searches_and_loads_full_specs_in_one_call(tmp_path) -> None:
    agent = _agent(tmp_path, tool_catalog_deferred_categories=["goal"])
    result = agent.tools.tools["tool_search"].execute(
        {"query": "get_goal 查看持续目标", "limit": 4}
    )

    assert result.ok
    loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
    assert "get_goal" in loaded
    payload = json.loads(result.output)
    by_name = {item["name"]: item for item in payload["tools"]}
    assert "get_goal" in by_name
    assert "properties" in by_name["get_goal"]["input_schema"]
    assert by_name["get_goal"]["schema_hash"].startswith("sha256:")
    assert payload["mode"] == "search_and_load"
    assert payload["loaded_for_next_model_call"] == loaded
    assert "next_step" not in payload
    names = {
        spec.name
        for spec in agent.tools.model_visible_specs(loaded_tool_names=set(loaded))
    }
    assert "get_goal" in names
    assert "create_subagents" in names


def test_tool_search_schema_has_only_query_and_limit(tmp_path) -> None:
    agent = _agent(tmp_path)
    schema = agent.tools.tools["tool_search"].model_spec.input_schema

    assert set(schema["properties"]) == {"query", "limit"}
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False


# LLM: 跑实际请求周期而非旧私有清理helper；所有后端响应为本地固定值，不请求模型。
# 函数用途: 用最小绑定能力检查临时工具集合在响应边的消费规则。
def _request_with_loaded_tools(loaded, *, status="completed", visible=True):
    return request_model_response(
        build_prompt=lambda: "原请求",
        generate_response=lambda _prompt: ModelResponse("", "fake", runtime_status=status),
        restore_rejected_input=lambda: None,
        recover_context=lambda _prompt: False,
        read_overflow_retry_limit=lambda: 0,
        visible_loaded_tools=loaded if visible else None,
    )


def test_loaded_tool_schema_is_consumed_after_one_successful_model_call() -> None:
    loaded = {"get_goal"}
    _request_with_loaded_tools(loaded)
    assert loaded == set()


def test_loaded_tool_schema_survives_overflow_or_auxiliary_reply() -> None:
    overflow, auxiliary = {"get_goal"}, {"get_goal"}
    _request_with_loaded_tools(overflow, status="context_overflow")
    _request_with_loaded_tools(auxiliary, visible=False)
    assert overflow == {"get_goal"}
    assert auxiliary == {"get_goal"}


def test_explicit_allowed_tools_are_structured_direct_exposure(tmp_path) -> None:
    agent = _agent(tmp_path)
    names = {
        spec.name
        for spec in agent.tools.model_visible_specs(
            allowed_tools=["inspect_agent_tree", "get_goal"]
        )
    }
    assert names == {"get_goal"}


def test_core_tools_stay_in_main_catalog(tmp_path) -> None:
    """EXEC-31b: native 下目录不展开条目(Schema 走原生通道),主目录只有说明行。"""
    agent = _agent(tmp_path)
    body = agent.tools.render_catalog_section()
    assert "原生工具通道" in body


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
    assert "get_goal" in result.output
    assert "create_goal" in result.output


def test_empty_deferred_restores_full_catalog(tmp_path) -> None:
    """EXEC-31b: native 下 deferred=[] 也不展开条目(展开只属已删的 text 渲染)。"""
    agent = _agent(tmp_path, tool_catalog_deferred_categories=[])
    section = agent.tools.render_catalog_section()
    assert "原生工具通道" in section
    assert "raise_collaboration" not in section


def test_deferred_shrinks_catalog_token_footprint(tmp_path) -> None:
    """EXEC-31b: native 下目录不展开, folded 与 full 正文相同(瘦身由原生 Schema 通道承担)。"""
    folded = _agent(tmp_path).tools.render_catalog_section()
    full = _agent(tmp_path, tool_catalog_deferred_categories=[]).tools.render_catalog_section()
    assert "原生工具通道" in folded
    # deferred 提示行是唯一长度差(条目展开已随 text 渲染删除)
    assert abs(len(folded) - len(full)) < 600  # 差=deferred 提示行(条目展开已删)


def test_native_catalog_does_not_duplicate_provider_schemas(tmp_path) -> None:
    agent = _agent(tmp_path)
    native = agent.tools.render_catalog_section(tool_protocol="native")

    assert "结构化 Schema 为准" in native
    # EXEC-31b: 目录不展开 "[name 分类] 参数" 形式的工具条目
    assert "[filesystem" not in native and "[api" not in native and "[web" not in native
