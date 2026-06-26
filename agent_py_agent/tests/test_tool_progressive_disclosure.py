from __future__ import annotations

"""工具渐进式披露单测 —— collaboration 等垂直 category 收起主目录,靠推荐区/list_tools 按需浮现。

背景:每轮 prompt 原本把全部工具的完整 spec 平铺进主目录(`render_catalog_section`),
一句问候也背着一堆垂直工具 → token 膨胀。本特性把 deferred category(默认 collaboration)从主目录
正文挪出,只留一行折叠名单;相关工具仍能被 vector 推荐区按任务拉回、被 list_tools 查到、按名直接
调用。普通对话大幅瘦身,垂直任务功能不丢,deferred=[] 完全恢复老行为。
"""

import tempfile

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
    """collaboration 工具的完整 spec 不在主目录正文,但末尾折叠行列出其名字(模型可按名直接调)。"""
    agent = _agent(tmp_path)
    section = agent.tools.render_catalog_section()
    assert "⊞" in section  # 折叠行存在
    body, fold = section.split("⊞", 1)
    # 垂直工具的完整条目不在主目录正文(瘦身的关键)
    assert "raise_collaboration" not in body
    assert "inspect_collaboration" not in body
    # 但折叠名单列出它们,模型据此可按名直接调用
    assert "raise_collaboration" in fold
    assert "inspect_collaboration" in fold


def test_core_tools_stay_in_main_catalog(tmp_path) -> None:
    """通用核心工具(文件/命令)照常在主目录正文,不受折叠影响。"""
    agent = _agent(tmp_path)
    body = agent.tools.render_catalog_section().split("⊞", 1)[0]
    assert "read_file" in body
    assert "run_command" in body


def test_deferred_tools_surface_via_recommended(tmp_path) -> None:
    """做协作任务时,相关 collaboration 工具被 vector 推荐区按 query 拉回 → 功能不丢。"""
    agent = _agent(tmp_path)
    rec = agent.tools.render_recommended_tools_section("发起跨代理协作 协作请求 collaboration 协助")
    assert "collaboration" in rec


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
