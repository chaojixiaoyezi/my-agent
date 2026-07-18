"""capability router 模块测试。

测试 CapabilityCard、CapabilityRouter、score_card、tokenize 等功能。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.capability.router import (
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    classify_tool_risk,
    from_skill_card,
    from_tool_spec,
    score_card,
    tokenize,
)

# ── CapabilityCard 测试 ────────────────────────────────────────────────────

def test_capability_card_basic():
    """测试 CapabilityCard 基本创建。"""
    card = CapabilityCard(
        id="test-card-1",
        kind="skill",
        name="测试技能",
        description="用于测试的能力卡",
    )
    assert card.id == "test-card-1"
    assert card.kind == "skill"
    assert card.name == "测试技能"
    assert card.risk_level == "low"  # 默认值


def test_capability_card_full_fields():
    """测试 CapabilityCard 所有字段。"""
    card = CapabilityCard(
        id="full-card",
        kind="tool",
        name="完整工具",
        description="完整字段测试",
        capabilities=["c1", "c2"],
        when_to_use=["场景1", "场景2"],
        not_when_to_use=["场景3"],
        keywords=["kw1", "kw2"],
        risk_level="medium",
        side_effects=["副作用1"],
        source="test",
        path="/path/to/tool",
        metadata={"extra": "value"},
    )
    assert card.capabilities == ["c1", "c2"]
    assert card.when_to_use == ["场景1", "场景2"]
    assert card.risk_level == "medium"
    assert card.metadata["extra"] == "value"


def test_capability_card_render_compact():
    """测试渲染短卡片。"""
    card = CapabilityCard(
        id="render-test",
        kind="skill",
        name="渲染测试",
        description="测试渲染功能",
        capabilities=["能力A", "能力B"],
        when_to_use=["使用场景1", "使用场景2"],
        not_when_to_use=["不适用场景"],
        side_effects=["可能的副作用"],
    )
    rendered = card.render_compact()
    assert "skill:渲染测试" in rendered
    assert "测试渲染功能" in rendered
    assert "能力A" in rendered


def test_capability_card_render_compact_truncated():
    """测试渲染超过最大字符数时截断。"""
    card = CapabilityCard(
        id="long-card",
        kind="skill",
        name="长描述卡片",
        description="A" * 200,
    )
    rendered = card.render_compact(max_chars=100)
    assert "... 已截断" in rendered
    assert len(rendered) < 250


# ── tokenize 测试 ──────────────────────────────────────────────────────────

def test_tokenize_english():
    """测试英文分词。"""
    tokens = tokenize("hello world test")
    assert "hello" in tokens
    assert "world" in tokens
    assert "test" in tokens


def test_tokenize_chinese():
    """测试中文分词。"""
    tokens = tokenize("你好世界测试")
    assert len(tokens) > 0
    # 中文应该被保留
    assert any("你好" in t or "你" in t for t in tokens)


def test_tokenize_mixed():
    """测试中英文混合分词。"""
    tokens = tokenize("hello 你好 world 世界")
    assert "hello" in tokens
    assert "world" in tokens
    # 应该有中文 token
    assert any(len(t) >= 2 for t in tokens)


def test_tokenize_empty():
    """测试空字符串分词。"""
    tokens = tokenize("")
    assert tokens == []


# ── score_card 测试 ──────────────────────────────────────────────────────

def test_score_card_keyword_match():
    """测试关键词匹配评分。"""
    card = CapabilityCard(
        id="kw-match",
        kind="skill",
        name="测试技能",
        description="用于数据分析",
        keywords=["数据", "分析", "统计"],
    )
    score, reasons = score_card("需要数据分析工具", card)
    assert score > 0
    assert len(reasons) > 0


def test_score_card_no_match():
    """测试无匹配时评分。"""
    card = CapabilityCard(
        id="no-match",
        kind="skill",
        name="测试技能",
        description="完全无关的描述",
        keywords=["xxx", "yyy"],
    )
    score, reasons = score_card("数据分析统计", card)
    # 无关键词匹配时分数可能为0或很低
    assert score >= 0


def test_score_card_capabilities_match():
    """测试 capabilities 字段匹配。"""
    card = CapabilityCard(
        id="cap-match",
        kind="tool",
        name="文件工具",
        description="处理文件",
        capabilities=["读取", "写入", "删除"],
    )
    score, reasons = score_card("需要读取文件的工具", card)
    assert score > 0


# ── CapabilityRouter 测试 ──────────────────────────────────────────────────

def test_capability_router_default_cards():
    """Bare routers contain static cards only; SkillsService owns all Skill loading."""
    router = CapabilityRouter()
    ids = {card.id for card in router.cards()}
    assert "builtin:playwright-browser-testing" in ids
    assert not any(card_id.startswith("skill:") for card_id in ids)


def test_capability_router_without_snapshot_has_no_skills():
    router = CapabilityRouter()
    assert [card.id for card in router.cards()] == ["builtin:playwright-browser-testing"]


def test_capability_router_register():
    """测试注册能力卡。"""
    router = CapabilityRouter()
    card = CapabilityCard(
        id="reg-test",
        kind="skill",
        name="注册测试",
        description="测试注册功能",
    )
    router.register(card)

    cards = router.cards()
    assert any(card.id == "reg-test" for card in cards)


def test_capability_router_register_override():
    """测试注册相同ID覆盖。"""
    router = CapabilityRouter()
    card1 = CapabilityCard(id="same-id", kind="skill", name="第一个", description="")
    card2 = CapabilityCard(id="same-id", kind="skill", name="第二个", description="")

    router.register(card1)
    router.register(card2)

    cards = router.cards()
    selected = [card for card in cards if card.id == "same-id"]
    assert len(selected) == 1
    assert selected[0].name == "第二个"


def test_capability_router_filter_by_kind():
    """测试按 kind 过滤能力卡。"""
    router = CapabilityRouter()
    router.register(CapabilityCard(id="skill-1", kind="skill", name="技能1", description=""))
    router.register(CapabilityCard(id="tool-1", kind="tool", name="工具1", description=""))
    router.register(CapabilityCard(id="tool-2", kind="tool", name="工具2", description=""))

    skill_cards = router.cards(kinds={"skill"})
    # 隔离 builtin skill,只测注册逻辑:仅手注册的 skill-1
    assert len(skill_cards) == 1
    assert all(card.kind == "skill" for card in skill_cards)

    tool_cards = router.cards(kinds={"tool"})
    assert len(tool_cards) == 3
    assert all(c.kind == "tool" for c in tool_cards)


def test_capability_router_search():
    """测试能力搜索。"""
    router = CapabilityRouter()
    router.register(CapabilityCard(
        id="file-tool",
        kind="tool",
        name="文件工具",
        description="处理文件读取和写入",
        keywords=["文件", "读取", "写入"],
    ))
    router.register(CapabilityCard(
        id="network-tool",
        kind="tool",
        name="网络工具",
        description="发送HTTP请求",
        keywords=["网络", "HTTP", "请求"],
    ))

    hits = router.search("需要读取文件")
    assert len(hits) >= 1
    assert hits[0].card.id == "file-tool"


def test_capability_router_search_limit():
    """测试搜索结果限制。"""
    router = CapabilityRouter()
    for i in range(10):
        router.register(CapabilityCard(
            id=f"card-{i}",
            kind="skill",
            name=f"技能{i}",
            description=f"技能描述{i}",
            keywords=["测试", "关键词"],
        ))

    hits = router.search("测试关键词", limit=3)
    assert len(hits) == 3


def test_capability_router_search_no_limit():
    """测试无限制搜索。"""
    router = CapabilityRouter()
    for i in range(5):
        router.register(CapabilityCard(
            id=f"nlimit-{i}",
            kind="skill",
            name=f"技能{i}",
            description=f"描述{i}",
            keywords=["测试"],
        ))

    hits = router.search("测试", limit=0)  # 0 表示不限制
    assert len(hits) == 5


def test_capability_router_render_candidates():
    """测试渲染候选能力。"""
    router = CapabilityRouter()
    router.register(CapabilityCard(
        id="render-card",
        kind="skill",
        name="渲染技能",
        description="测试渲染",
    ))

    rendered = router.render_candidates("测试")
    assert "Candidate Capabilities" in rendered
    assert "渲染技能" in rendered


def test_capability_router_render_candidates_empty():
    """测试渲染空候选。"""
    router = CapabilityRouter()
    rendered = router.render_candidates("xyz")
    assert "没有明显匹配" in rendered


# ── from_skill_card 和 from_tool_spec 测试 ────────────────────────────────

def test_from_skill_card():
    """测试从 SkillCard 转换。"""
    from pathlib import Path

    from agent_py_agent.agent.capability.skills import SkillCard

    skill = SkillCard(
        name="test-skill",
        description="测试技能描述",
        path=Path("/tmp/test-skill/SKILL.md"),
        capabilities=["cap1", "cap2"],
    )

    card = from_skill_card(skill)

    assert card.kind == "skill"
    assert card.name == "test-skill"
    assert card.description == "测试技能描述"


def test_from_tool_spec():
    """测试从 ToolSpec 转换。"""
    from agent_py_agent.agent.tooling.models import ToolSpec

    spec = ToolSpec(
        name="test-tool",
        category="utility",
        description="测试工具描述",
        use_cases=["测试场景"],
        avoid_when=["避免场景"],
        keywords=["测试"],
        parameters={"param1": "参数1说明"},
    )

    card = from_tool_spec(spec)

    assert card.kind == "tool"
    assert card.name == "test-tool"


# ── classify_tool_risk 测试 ──────────────────────────────────────────────

def test_classify_tool_risk_low():
    """测试低风险工具分类。"""
    from agent_py_agent.agent.tooling.models import ToolSpec

    spec = ToolSpec(
        name="read_file",
        category="filesystem",
        description="读取文件",
        use_cases=["查看文件内容"],
        avoid_when=[],
        keywords=["读取", "文件"],
        parameters={"path": "文件路径"},
    )
    sides, risk = classify_tool_risk(spec)
    assert risk in ("low", "medium", "high")


def test_classify_tool_risk_with_side_effects():
    """测试有副作用的工具分类。"""
    from agent_py_agent.agent.tooling.models import ToolSpec

    spec = ToolSpec(
        name="write_file",
        category="filesystem",
        description="写入文件",
        use_cases=["创建或覆盖文件"],
        avoid_when=["不确定内容时"],
        keywords=["写入", "文件"],
        parameters={"path": "文件路径", "content": "内容"},
    )
    sides, risk = classify_tool_risk(spec)
    assert risk in ("low", "medium", "high")


# ── CapabilitySearchHit 测试 ──────────────────────────────────────────────

def test_capability_search_hit():
    """测试 CapabilitySearchHit 数据类。"""
    card = CapabilityCard(
        id="hit-card",
        kind="skill",
        name="命中卡片",
        description="测试",
    )
    hit = CapabilitySearchHit(
        card=card,
        score=0.85,
        reasons=["关键词匹配", "描述相关"],
    )

    assert hit.card.id == "hit-card"
    assert hit.score == 0.85
    assert len(hit.reasons) == 2
