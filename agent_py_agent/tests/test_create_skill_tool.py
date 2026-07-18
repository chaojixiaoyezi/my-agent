"""create_skill 自学习工具钉子。严守 AGENTS.md 自学习约束:
① 仅 enable_self_learning=true 可用(默认关闭就禁用);
② agent 绝不直接写正式 skill 库,只产 owner skills/.drafts/ 下的草稿、不 register;
③ 正式 owner skills 库只由用户确认后写入,下一轮由唯一 SkillsService 快照召回。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import agent_py_agent.agent.capability.create_skill_tool as mod
from agent_py_agent.agent.capability.create_skill_tool import (
    CreateSkillTool,
    _render_skill_md,
    _slug,
)
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer


def _agent(tmp_path, *, enabled=True, router=None):
    return SimpleNamespace(
        config=SimpleNamespace(enable_self_learning=enabled),
        root=tmp_path,
        capability_router=router or CapabilityRouter(),
    )


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


def test_disabled_when_self_learning_off(tmp_path, monkeypatch):
    """enable_self_learning 默认关闭时 create_skill 直接禁用,不写任何东西(零越权)。"""
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path / "owner")
    tool = CreateSkillTool(_agent(tmp_path, enabled=False))
    result = tool.execute(
        {"name": "x", "category": "research", "description": "d", "when_to_use": "w", "body": "# b"}
    )
    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"
    assert not (tmp_path / "data" / "skill_drafts").exists()


def test_missing_required_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path / "owner")
    tool = CreateSkillTool(_agent(tmp_path))
    result = tool.execute({"name": "", "description": "", "body": ""})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_create_skill_writes_draft_not_official(tmp_path, monkeypatch):
    """enable 后:写到 owner skills/.drafts/,绝不写正式 owner skills 库,也不 register 到 router。"""
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: tmp_path / "owner")
    router = CapabilityRouter()
    tool = CreateSkillTool(_agent(tmp_path, router=router))
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
    # 草稿落 skill_drafts,不落正式库
    draft = tmp_path / "owner" / "skills" / ".drafts" / "research" / "arxiv-fetch" / "SKILL.md"
    assert draft.is_file()
    assert "arXiv" in draft.read_text(encoding="utf-8")
    official = tmp_path / "owner" / "skills" / "research" / "arxiv-fetch" / "SKILL.md"
    assert not official.exists()
    # 不 register:本 run 的 router 检索不到(未经用户确认绝不生效)
    assert not any(h.card.name == "arxiv-fetch" for h in router.search("arxiv 检索论文", limit=5, kinds={"skill"}))


def test_create_skill_quota_rejects_before_draft_write(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    monkeypatch.setattr(mod, "runtime_owner_root", lambda agent: owner)
    agent = _agent(tmp_path)
    agent.owner_quota = OwnerQuotaEnforcer(owner, max_bytes=1)

    result = CreateSkillTool(agent).execute(
        {"name": "large", "description": "d", "body": "# body\ncontent"}
    )

    assert result.ok is False
    assert result.error_code == "OWNER_DISK_QUOTA_EXCEEDED"
    assert not (owner / "skills" / ".drafts").exists()


def test_skills_service_discovers_confirmed_owner_skill_next_turn(tmp_path, skill_catalog_factory):
    catalog = skill_catalog_factory(tmp_path / "home")
    owner = Path(catalog.home.owner_home_dir)
    skill = owner / "skills" / "ops" / "log-triage" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        _render_skill_md(
            {"name": "log-triage", "description": "大日志三段定位法", "when_to_use": "排查大日志找错误时", "category": "ops"},
            "# 步骤\n1. grep ERROR\n2. 看前后文",
        ),
        encoding="utf-8",
    )
    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    fresh = CapabilityRouter(skill_snapshot=snapshot)
    assert any(h.card.name == "log-triage" for h in fresh.search("大日志 排查错误", limit=5, kinds={"skill"}))


def test_skills_service_new_owner_without_skills_is_empty(tmp_path, skill_catalog_factory):
    catalog = skill_catalog_factory(tmp_path / "home")
    assert catalog.snapshot.enabled_entries() == ()
