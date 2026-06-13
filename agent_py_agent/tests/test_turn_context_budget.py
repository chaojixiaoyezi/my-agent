"""批3 单回合聚合预算钉子(长期助手 turn budget 蓝本)。

钉死契约:本轮工具输出累计超 ~200K 字符时,从最大段开始裁到安全份额,
裁口落换行处并带恢复提示;预算内一字不动;只动本轮新增段。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _enforce_turn_context_budget,
)


def test_under_budget_untouched():
    params = SimpleNamespace(tool_context=["旧段", "本轮段A" * 100, "本轮段B" * 100])
    snapshot = list(params.tool_context)
    _enforce_turn_context_budget(params, 1)
    assert params.tool_context == snapshot


def test_over_budget_clips_largest_at_newline_with_hint():
    huge = ("数据行内容" * 30 + "\n") * 2000  # ~300K 字符,带换行
    small = "小结果" * 50
    params = SimpleNamespace(tool_context=["旧段不可动" * 9000, huge, small])
    _enforce_turn_context_budget(params, 1)
    clipped = params.tool_context[1]
    assert len(clipped) < len(huge), "最大段被裁"
    assert "已截断" in clipped and "重新调用该工具" in clipped, "裁口带恢复提示"
    body = clipped.split("\n... [本轮工具输出总量超预算")[0]
    assert body.endswith("内容") is False or body.endswith("\n") is False  # 裁口在行边界:
    assert body == body.rstrip("\r") and body.rfind("\n") > 0
    assert params.tool_context[2] == small, "小段不动"
    assert params.tool_context[0].startswith("旧段不可动"), "本轮之前的段绝不动"
    assert len(params.tool_context[0]) == 9000 * 5


def test_missing_or_short_context_safe():
    _enforce_turn_context_budget(SimpleNamespace(tool_context=None), 0)
    _enforce_turn_context_budget(SimpleNamespace(tool_context=[]), 0)
    params = SimpleNamespace(tool_context=["全是旧段" * 100000])
    _enforce_turn_context_budget(params, 1)
    assert len(params.tool_context) == 1
