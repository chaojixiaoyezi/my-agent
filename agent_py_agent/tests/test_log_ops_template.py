
from __future__ import annotations

"""日志模板挖掘单测 —— ML 前期把海量日志无监督聚类成"模板+占比",喂 LLM 备课。"""

from agent_py_agent.agent.tooling.log_ops.log_template import mine_templates


def test_similar_lines_merge_to_template() -> None:
    lines = ["user alice login ok", "user bob login ok", "user carol login ok"]
    templates = mine_templates(lines)
    assert len(templates) == 1
    assert templates[0]["template"] == "user <*> login ok"  # 变化的用户名 → <*>
    assert templates[0]["count"] == 3
    assert templates[0]["share"] == 1.0


def test_different_structures_separate() -> None:
    templates = mine_templates(["GET /a 200", "GET /b 200", "ERROR db timeout after 5s"])
    temps = {t["template"] for t in templates}
    assert "GET <*> 200" in temps  # 两条 GET 归一类
    assert any("ERROR" in t for t in temps)  # 不同长度的 ERROR 行单独


def test_count_descending_and_share() -> None:
    templates = mine_templates(["a x"] * 5 + ["b y"] * 2, sim_threshold=0.5)
    assert templates[0]["count"] == 5  # 最多的排前
    assert templates[0]["share"] == round(5 / 7, 4)


def test_max_templates_cap() -> None:
    lines = [f"uniqueA{i} uniqueB{i} uniqueC{i}" for i in range(100)]  # 全不同
    assert len(mine_templates(lines, max_templates=10, sim_threshold=0.95)) <= 10


def test_empty_lines_skipped() -> None:
    assert mine_templates(["", "  ", "real line here"]) == [
        {"template": "real line here", "count": 1, "share": 1.0, "example": "real line here"}
    ]
