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
4. 主 run prompt 在有界预算内注入 name、description、stable id；具体 Skill
   正文必须通过结构化 skill_search 读取，不允许关键词分数替模型自动注卡；
   router 缺席整段缺席。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
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


def test_category_derivation_nested_flat_and_override(tmp_path: Path, skill_catalog_factory) -> None:
    skill_root = tmp_path / "skills"
    _make_skill(skill_root, "research/code", "analyzer", "深度分析")
    _make_skill(skill_root, "", "loner", "平铺技能")
    d = skill_root / "docs" / "translator"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: translator\ndescription: 翻译\ncategory: custom/zone\n---\n", encoding="utf-8")
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root])
    by_name = {entry.name: entry for entry in catalog.snapshot.entries}
    assert by_name["analyzer"].category == "research/code", "嵌套目录推导类目链"
    assert by_name["loner"].category == "general", "平铺兼容"
    assert by_name["translator"].category == "custom/zone", "frontmatter 显式覆盖"


def test_category_index_decoupled_from_skill_count(tmp_path: Path, skill_catalog_factory) -> None:
    skill_root = tmp_path / "skills"
    for i in range(30):
        _make_skill(skill_root, "research", f"s{i:02d}", f"技能{i}")
    for i in range(30):
        _make_skill(skill_root, "devops", f"d{i:02d}", f"运维{i}")
    router = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root]).router
    index = router.render_category_index()
    assert index.count("\n") <= 4, "60 个 skill 的索引仍只有类目行数(与总数解耦)"
    assert "research（30 个）" in index and "devops（30 个）" in index


def test_two_hundred_cards_scan_and_search_fast(tmp_path: Path, skill_catalog_factory) -> None:
    skill_root = tmp_path / "skills"
    for i in range(200):
        _make_skill(skill_root, f"cat{i % 8}", f"skill-{i:03d}", f"测试技能 数据处理 第{i}号")
    start = time.monotonic()
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root])
    router = catalog.router
    hits = router.search("数据处理", kinds={"skill"})
    elapsed = time.monotonic() - start
    assert len(catalog.snapshot.entries) == 200
    assert hits, "中文 query 命中"
    assert elapsed < 1.0, f"200 卡 scan+search 应远快于 1s,实测 {elapsed:.3f}s"


def test_prompt_injection_exposes_budgeted_metadata_but_not_skill_body(
    tmp_path: Path,
    skill_catalog_factory,
) -> None:
    skill_root = tmp_path / "skills"
    _make_skill(skill_root, "documents", "pdf-translate-toolchain", "把论文翻译成中文 PDF")
    router = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root]).router
    builder = SimpleNamespace(capability_router=router)
    hit_chunks = _skill_context_chunks(builder, "把论文翻译成中文 PDF")
    assert any("Available Skills" in c for c in hit_chunks)
    assert any("pdf-translate-toolchain" in c for c in hit_chunks)
    assert any("workspace:pdf-translate-toolchain" in c for c in hit_chunks)
    assert any("任务明确匹配" in c for c in hit_chunks)
    assert any("不能把读取、概括或解释 Skill 指令委派给子代理" in c for c in hit_chunks)
    assert not any("Matched Skills" in c for c in hit_chunks), "自然语言不得替模型自动选中 Skill"
    assert not any("# PDF 翻译工具链" in c for c in hit_chunks), "完整正文必须按 stable id 读取"
    chat_chunks = _skill_context_chunks(builder, "今天天气怎么样")
    assert any("Available Skills" in c for c in chat_chunks)
    assert not any("Matched Skills" in c for c in chat_chunks), "闲聊同样只留索引"
    assert _skill_context_chunks(SimpleNamespace(), "任意") == [], "router 缺席整段缺席"


def test_skill_get_returns_codex_style_source_locator(
    tmp_path: Path,
    skill_catalog_factory,
) -> None:
    skill_root = tmp_path / "skills"
    _make_skill(skill_root, "quality", "systematic-debugging", "先定位根因再修复")
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root])
    agent = SimpleNamespace(
        capability_router=catalog.router,
        current_skill_snapshot=lambda: catalog.snapshot,
    )

    result = SkillSearchTool(agent).execute(
        {"action": "get", "skill_id": "workspace:systematic-debugging"}
    )
    payload = json.loads(result.output)

    assert result.ok
    assert payload["path"] == str(
        skill_root / "quality" / "systematic-debugging" / "SKILL.md"
    )
    assert payload["body"].endswith("# systematic-debugging\n")


def test_skill_metadata_index_uses_two_percent_budget_and_keeps_stable_ids(
    tmp_path: Path,
    skill_catalog_factory,
) -> None:
    skill_root = tmp_path / "skills"
    for index in range(120):
        _make_skill(
            skill_root,
            "bulk",
            f"skill-{index:03d}",
            "这是用于验证受控 Skill metadata 预算的很长描述" * 20,
        )
    router = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root]).router

    rendered = router.render_skill_metadata_index(context_window_tokens=30_000)

    assert "Available Skills" in rendered
    assert "skill_id:" in rendered
    assert len(rendered.encode("utf-8")) < 8_000, "600-token 行预算加固定说明仍应保持有界"
    assert "预算不足" in rendered or "description 已" in rendered


@pytest.mark.parametrize(
    ("query", "expected_first"),
    [
        ("Python 项目测试偶发失败，先系统定位根因再修复", "systematic-debugging"),
        ("把这份英文论文翻译成中文 PDF 并保持排版", "pdf-translate-toolchain"),
        ("深入阅读并对比两个代码仓库的架构优缺点", "deep-code-analysis"),
        ("为整个代码仓库建立威胁模型和信任边界", "threat-model"),
        ("准备汇报任务已经完成，先运行验证并核对证据", "verification-before-completion"),
        ("有四个彼此独立且不共享状态的问题，交给多个子代理并行处理", "dispatching-parallel-agents"),
    ],
)
def test_builtin_skill_search_routes_representative_tasks_to_expected_method(
    tmp_path: Path,
    skill_catalog_factory,
    query: str,
    expected_first: str,
) -> None:
    """内置方法随发布安装后，常见任务应把正确 Skill 排在第一位。

    这只验证只读候选检索，不替模型自动选择，也不把自然语言命中提升为权限或执行判断。
    """

    catalog = skill_catalog_factory(tmp_path / "home")
    builtin_source = Path(__file__).resolve().parents[1] / "skills" / "builtin"
    shutil.copytree(builtin_source, catalog.home.shared_builtin_dir, dirs_exist_ok=True)
    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    router = CapabilityRouter(skill_snapshot=snapshot)

    assert len(snapshot.entries) == 26
    assert snapshot.errors == ()
    hits = router.search(query, kinds={"skill"}, limit=5)

    assert hits
    assert hits[0].card.name == expected_first
