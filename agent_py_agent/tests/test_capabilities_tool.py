
from __future__ import annotations

"""list_capabilities 单测 —— 能力动态发现(治造轮子)。

核心:通道列表从 adapter 注册表**自动汇总**(加新 adapter 自动出现,系统提示词永不改);能力区在代码、
不在提示词。这样安全纪律(提示词)与能力清单(注册表/代码)解耦。
"""

from agent_py_agent.agent.tooling.capabilities_tool import (
    ListCapabilitiesTool,
    _available_channels,
    build_capability_inventory,
)


def test_channels_auto_aggregated_from_adapter_registry() -> None:
    """通道从 adapter 模块自动汇总:含 feishu/qq,排除抽象基类 base(加新 adapter 自动出现,不用改代码/提示词)。"""
    chans = _available_channels()
    assert "feishu" in chans
    assert "qq" in chans
    assert "base" not in chans


def test_capability_inventory_covers_product_areas() -> None:
    """能力清单覆盖产品级能力(多通道/多用户/记忆/编排…),且明确"别造轮子用内置"——这是工具里(代码),不在系统提示词。"""
    inv = build_capability_inventory()
    areas = {c["area"] for c in inv["capabilities"]}
    assert "多通道网关" in areas
    assert "多用户隔离" in areas
    assert "造轮子" in inv["principle"]
    # 多通道的 how 明确不要自己搭 bot
    gateway = next(c for c in inv["capabilities"] if c["area"] == "多通道网关")
    assert "adapter start" in gateway["how"]


def test_list_capabilities_tool_executes() -> None:
    r = ListCapabilitiesTool().execute({})
    assert r.ok and r.result_envelope.get("channels") and r.result_envelope.get("capabilities")
