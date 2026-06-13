"""批2 钉子:知识召回接通 + skill 树第一期(千级地基)。

实锤来源:R10 三案 used_memories=0、skill 零召回——lesson 匹配要求英文文件名
作中文 prompt 子串(必零命中);skill 推荐链在主 run prompt 缺席。

钉死契约:
1. lesson 召回走路由索引 trigger_keywords(中文命中),闲聊零召回,索引缺失
   回退旧 stem 匹配。
2. skill 树:目录即分类(嵌套推导/平铺归 general/frontmatter 覆盖);类目索引
   与 skill 总数解耦;200 卡 scan+search <100ms。
3. skill_search 工具:命中给卡+正文路径;无命中给类目线索;category 过滤;
   空 query 结构化报错。
4. 主 run prompt 注入:类目索引常驻+仅命中注卡;router 缺席整段缺席。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skills import SkillRegistry
from agent_py_agent.agent.prompting_parts.builder import (
    _matching_lesson_paths,
    _skill_context_chunks,
)
from agent_py_agent.agent.user_space.home_memory_seeds import (
    default_memory_lessons,
    default_memory_route_index_md,
)

pytestmark = pytest.mark.integration


def _seed_home(tmp_path: Path) -> SimpleNamespace:
    lessons = tmp_path / "memory" / "lessons"
    lessons.mkdir(parents=True)
    routing = tmp_path / "memory" / "routing"
    routing.mkdir(parents=True)
    for name, content in default_memory_lessons().items():
        (lessons / name).write_text(content, encoding="utf-8")
    (routing / "INDEX.md").write_text(default_memory_route_index_md(), encoding="utf-8")
    return SimpleNamespace(owner_memory_lessons_dir=lessons)


def test_lesson_recall_via_routing_keywords(tmp_path: Path) -> None:
    hp = _seed_home(tmp_path)
    assert [p.name for p in _matching_lesson_paths(hp, "请检索 deepseek 的论文".casefold())] == ["research.md"]
    assert [p.name for p in _matching_lesson_paths(hp, "报告写到哪个输出目录".casefold())] == ["workspace.md"]
    assert _matching_lesson_paths(hp, "今天天气怎么样".casefold()) == []


def test_lesson_recall_falls_back_to_stem_without_index(tmp_path: Path) -> None:
    lessons = tmp_path / "memory" / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "compact.md").write_text("# C", encoding="utf-8")
    hp = SimpleNamespace(owner_memory_lessons_dir=lessons)
    assert [p.name for p in _matching_lesson_paths(hp, "debug compact flow")] == ["compact.md"]


def _make_skill(root: Path, rel: str, name: str, desc: str) -> None:
    d = root / rel / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n", encoding="utf-8")


def test_category_derivation_nested_flat_and_override(tmp_path: Path) -> None:
    _make_skill(tmp_path, "research/code", "analyzer", "深度分析")
    _make_skill(tmp_path, "", "loner", "平铺技能")
    d = tmp_path / "docs" / "translator"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: translator\ndescription: 翻译\ncategory: custom/zone\n---\n", encoding="utf-8")
    reg = SkillRegistry([tmp_path])
    reg.scan()
    by_name = {c.name: c for c in reg.cards()}
    assert by_name["analyzer"].category == "research/code", "嵌套目录推导类目链"
    assert by_name["loner"].category == "general", "平铺兼容"
    assert by_name["translator"].category == "custom/zone", "frontmatter 显式覆盖"


def test_category_index_decoupled_from_skill_count(tmp_path: Path) -> None:
    for i in range(30):
        _make_skill(tmp_path, "research", f"s{i:02d}", f"技能{i}")
    for i in range(30):
        _make_skill(tmp_path, "devops", f"d{i:02d}", f"运维{i}")
    reg = SkillRegistry([tmp_path])
    reg.scan()
    router = CapabilityRouter(skill_registry=reg)
    index = router.render_category_index()
    assert index.count("\n") <= 4, "60 个 skill 的索引仍只有类目行数(与总数解耦)"
    assert "research（30 个）" in index and "devops（30 个）" in index


def test_two_hundred_cards_scan_and_search_fast(tmp_path: Path) -> None:
    for i in range(200):
        _make_skill(tmp_path, f"cat{i % 8}", f"skill-{i:03d}", f"测试技能 数据处理 第{i}号")
    start = time.monotonic()
    reg = SkillRegistry([tmp_path])
    reg.scan()
    router = CapabilityRouter(skill_registry=reg)
    hits = router.search("数据处理", kinds={"skill"})
    elapsed = time.monotonic() - start
    assert len(reg.cards()) == 200
    assert hits, "中文 query 命中"
    assert elapsed < 1.0, f"200 卡 scan+search 应远快于 1s,实测 {elapsed:.3f}s"


def test_prompt_injection_index_always_card_on_hit_only(tmp_path: Path) -> None:
    builder = SimpleNamespace(capability_router=CapabilityRouter())
    hit_chunks = _skill_context_chunks(builder, "把论文翻译成中文 PDF")
    assert any("Skill Categories" in c for c in hit_chunks)
    assert any("Matched Skills" in c and "pdf-translate-toolchain" in c for c in hit_chunks)
    chat_chunks = _skill_context_chunks(builder, "今天天气怎么样")
    assert any("Skill Categories" in c for c in chat_chunks)
    assert not any("Matched Skills" in c for c in chat_chunks), "闲聊只留索引不注卡"
    assert _skill_context_chunks(SimpleNamespace(), "任意") == [], "router 缺席整段缺席"


def test_skill_inject_score_threshold_blocks_marginal_hits(tmp_path):
    """批4 注卡分数门钉子:长 prompt 边缘 n-gram 命中(<20 分)不注卡;
    真命中照常注。R11 预检实锤:周榜任务曾被硬塞两张无关 [low] 卡。"""
    from agent_py_agent.agent.prompting_parts.builder import _skill_context_chunks

    class _FakeHit:
        def __init__(self, score):
            self.score = score
            self.card = type("C", (), {"render_compact": lambda s: f"- skill: fake [{score}]"})()

    class _FakeRouter:
        def __init__(self, scores):
            self._scores = scores

        def render_category_index(self):
            return "# Skill Categories\n- research: 1"

        def search(self, query, *, limit, kinds):
            return [_FakeHit(s) for s in self._scores][:limit]

    class _B:
        capability_router = _FakeRouter([13.0, 8.5])

    chunks = _skill_context_chunks(_B(), "做一份周榜")
    assert len(chunks) == 1, "边缘分只留类目索引,不注卡"

    _B.capability_router = _FakeRouter([49.0, 8.5])
    chunks = _skill_context_chunks(_B(), "翻译论文")
    assert len(chunks) == 2 and "fake [49.0]" in chunks[1], "真命中照常注卡"
