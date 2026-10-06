"""learnpack 第 5 步：内置技能 learn-external-agent。

锁定：技能能被索引并读到正文；名片写短；需要的工具在管理员主代理的工具表里都存在；正文写明许可证、学习笔记、默认能力包、
照回执提醒用户、不替用户确认这些要点；参考文件链接都存在。
"""
from __future__ import annotations

import re
from pathlib import Path

from agent_py_agent.agent.capability.skills import parse_skill_file
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig

SKILL = Path(__file__).resolve().parents[1] / "skills" / "builtin" / "plugins" / "learn-external-agent"


def test_card_is_short_and_body_has_the_key_rules():
    card = parse_skill_file(SKILL / "SKILL.md", source="builtin", require_frontmatter=True)
    assert card.name == "learn-external-agent" and len(card.description) <= 80 and card.when_to_use
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for token in ("learnpack-notes.md", "MIT", "GPL", "package_build", "package_install", "照回执原文",
                  "不替用户执行任何确认命令", "默认做能力包", "skill_search"):
        assert token in body, token


def test_reference_links_exist():
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\((references/[^)]+)\)", body):
        assert (SKILL / link).is_file(), link
    assert (SKILL / "templates" / "CAPABILITY.md").is_file() and (SKILL / "templates" / "PROVENANCE.md").is_file()


def test_required_tools_exist_for_the_local_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                    my_agent_owner_kind="main", my_agent_owner_id="main"), tmp_path / "project")
    card = parse_skill_file(SKILL / "SKILL.md", source="builtin", require_frontmatter=True)
    missing = set(card.tools_required) - set(agent.tools.tools)
    assert not missing, missing
