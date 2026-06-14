"""create_skill 自学习工具钉子(对标 长期助手 自动创建 skill):agent 把可复用方法
写成 SKILL.md 进 owner skill 库,写后立即注册到 router、本 run 即可 skill_search 召回。"""

from __future__ import annotations

from types import SimpleNamespace

import agent_py_agent.agent.capability.create_skill_tool as mod
from agent_py_agent.agent.capability.create_skill_tool import (
    CreateSkillTool,
    _render_skill_md,
    _slug,
)
from agent_py_agent.agent.capability.router import CapabilityRouter


def test_slug_normalizes():
    assert _slug("Deep Code Analysis") == "deep-code-analysis"
    assert _slug("  arXiv-Fetch! ") == "arxiv-fetch"
    assert _slug("研究 方法") == "研究-方法"


def test_render_skill_md_frontmatter():
    md = _render_skill_md(
        {"name": "x-skill", "description": "一句话描述", "when_to_use": "某场景", "category": "research"},
        "# 方法\n1. a",
    )
    lines = md.splitlines()
    assert lines[0] == "---"
    assert "name: x-skill" in lines
    assert "description: 一句话描述" in lines
    assert "when_to_use: 某场景" in lines
    assert "category: research" in lines
    assert "# 方法" in md


def test_missing_required_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path)
    tool = CreateSkillTool(SimpleNamespace(capability_router=CapabilityRouter()))
    result = tool.execute({"name": "", "description": "", "body": ""})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_create_skill_writes_and_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path)
    router = CapabilityRouter()
    tool = CreateSkillTool(SimpleNamespace(capability_router=router))
    result = tool.execute(
        {
            "name": "arxiv-fetch",
            "category": "research",
            "description": "从 arXiv 全字段检索最新论文",
            "when_to_use": "找特定主题最新论文时",
            "body": "# 方法\n1. site:arxiv.org + 主题词\n2. 日期倒序",
        }
    )
    assert result.ok
    # SKILL.md 落到 owner skills 目录
    target = tmp_path / "skills" / "research" / "arxiv-fetch" / "SKILL.md"
    assert target.is_file()
    assert "arXiv" in target.read_text(encoding="utf-8")
    # 写后立即注册到 router,本 run 即可召回
    hits = router.search("arxiv 检索论文", limit=5, kinds={"skill"})
    assert any(h.card.name == "arxiv-fetch" for h in hits)


def test_register_owner_skills_cross_run(tmp_path, monkeypatch):
    """跨 run 持久:run1 create_skill 落 owner 库,run2 全新 router 经
    register_owner_skills 补扫 owner 库即可召回(否则下次启动就'忘了',自学习无意义)。"""
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path)
    # run1:沉淀一个 skill
    CreateSkillTool(SimpleNamespace(capability_router=CapabilityRouter())).execute(
        {
            "name": "log-triage",
            "category": "ops",
            "description": "大日志三段定位法",
            "when_to_use": "排查大日志找错误时",
            "body": "# 步骤\n1. grep ERROR\n2. 看前后文",
        }
    )
    # run2:全新 router(默认只扫 builtin),启动补扫 owner 库
    fresh = CapabilityRouter()
    assert not any(h.card.name == "log-triage" for h in fresh.search("日志 定位", limit=5, kinds={"skill"}))
    count = mod.register_owner_skills(fresh, SimpleNamespace())
    assert count >= 1
    hits = fresh.search("大日志 排查错误", limit=5, kinds={"skill"})
    assert any(h.card.name == "log-triage" for h in hits)


def test_register_owner_skills_no_dir_safe(tmp_path, monkeypatch):
    """owner skills 目录不存在(全新用户)时静默返回 0,不崩。"""
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path / "nope")
    assert mod.register_owner_skills(CapabilityRouter(), SimpleNamespace()) == 0
