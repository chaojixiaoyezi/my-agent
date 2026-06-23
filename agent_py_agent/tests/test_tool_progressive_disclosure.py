from __future__ import annotations

"""工具渐进式披露单测 —— log_ops 等垂直 category 收起主目录,靠推荐区/list_tools 按需浮现。

背景:每轮 prompt 原本把全部 75 个工具的完整 spec 平铺进主目录(`render_catalog_section`),
一句问候也背着 30+ 个日志监控工具 → ~12700 token。本特性把 deferred category(默认 log_ops/
log_analysis)从主目录正文挪出,只留一行折叠名单;相关工具仍能被 vector 推荐区按任务拉回、被
list_tools 查到、按名直接调用。普通对话大幅瘦身,日志任务功能不丢,deferred=[] 完全恢复老行为。
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


def test_deferred_log_tools_collapsed_in_main_catalog(tmp_path) -> None:
    """log_ops 工具的完整 spec 不在主目录正文,但末尾折叠行列出其名字(模型可按名直接调)。"""
    agent = _agent(tmp_path)
    section = agent.tools.render_catalog_section()
    assert "⊞" in section  # 折叠行存在
    body, fold = section.split("⊞", 1)
    # 垂直工具的完整条目不在主目录正文(瘦身的关键)
    assert "log_monitor_start" not in body
    assert "log_ml_analyze" not in body
    # 但折叠名单列出它们,模型据此可按名直接调用
    assert "log_monitor_start" in fold
    assert "log_ml_analyze" in fold


def test_core_tools_stay_in_main_catalog(tmp_path) -> None:
    """通用核心工具(文件/命令)照常在主目录正文,不受折叠影响。"""
    agent = _agent(tmp_path)
    body = agent.tools.render_catalog_section().split("⊞", 1)[0]
    assert "read_file" in body
    assert "run_command" in body


def test_deferred_tools_surface_via_recommended(tmp_path) -> None:
    """做日志任务时,相关 log 工具被 vector 推荐区按 query 拉回 → 功能不丢。"""
    agent = _agent(tmp_path)
    rec = agent.tools.render_recommended_tools_section("帮我设置日志监控 采集告警 盯住日志异常")
    assert "log_" in rec


def test_normal_query_recommended_stays_small(tmp_path) -> None:
    """普通问候:推荐区不凑不相关的 log 工具(retriever score>0 过滤),prompt 维持精简。"""
    agent = _agent(tmp_path)
    rec = agent.tools.render_recommended_tools_section("请只回复四个字")
    assert rec.count("log_") == 0


def test_list_tools_still_lists_all_including_deferred(tmp_path) -> None:
    """list_tools 始终列全部工具(含被折叠的),作为兜底发现入口。"""
    agent = _agent(tmp_path)
    result = agent.tools.tools["list_tools"].execute({})
    assert result.ok
    assert "log_monitor_start" in result.output
    assert "log_ml_analyze" in result.output


def test_empty_deferred_restores_full_catalog(tmp_path) -> None:
    """deferred=[] 完全恢复老行为:全部工具铺主目录,无折叠行(向后兼容)。"""
    agent = _agent(tmp_path, tool_catalog_deferred_categories=[])
    section = agent.tools.render_catalog_section()
    assert "log_monitor_start" in section  # 垂直工具回到主目录正文
    assert "⊞" not in section  # 没有折叠行


def test_deferred_shrinks_catalog_token_footprint(tmp_path) -> None:
    """量化:开启 deferred 后主目录显著小于全量(瘦身真实发生,不是只挪位置)。"""
    folded = _agent(tmp_path).tools.render_catalog_section()
    full = _agent(tmp_path, tool_catalog_deferred_categories=[]).tools.render_catalog_section()
    assert len(folded) < len(full) * 0.7  # 至少省 30%+
