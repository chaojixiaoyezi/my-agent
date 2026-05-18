from __future__ import annotations

"""LLM: tests for draft-to-active skill lifecycle management.

给人看的解释：
这些测试覆盖 skill 从草稿到正式、禁用和回滚的本地文件闭环，保证现有
SkillRegistry 只扫描已晋级的 active skill。
"""

from pathlib import Path

from agent_py_agent.agent.capability.skills import (
    SkillDraftRequest,
    SkillLifecycleStore,
    SkillRegistry,
)


def test_skill_lifecycle_promotes_draft_into_active_registry(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    draft = store.create_draft(
        SkillDraftRequest(
            name="pytest-debug",
            markdown=_skill_text("pytest-debug", "Debug pytest failures", "v1 body"),
            reason="learned from accepted pytest task",
        )
    )
    registry = SkillRegistry([store.active_dir])

    registry.scan()
    promoted = store.promote("pytest-debug")
    registry.scan()

    assert draft.status == "draft"
    assert registry.get("pytest-debug").description == "Debug pytest failures"
    assert promoted.version == 1
    assert [event.action for event in store.events()] == ["draft_created", "promoted"]


def test_skill_lifecycle_disable_and_rollback_restore_previous_version(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    store.create_draft(SkillDraftRequest("release-notes", _skill_text("release-notes", "Draft releases", "v1")))
    store.promote("release-notes")
    store.create_draft(SkillDraftRequest("release-notes", _skill_text("release-notes", "Draft releases v2", "v2")))
    store.promote("release-notes")
    registry = SkillRegistry([store.active_dir])

    registry.scan()
    disabled = store.disable("release-notes", reason="too broad")
    registry.scan()
    rolled_back = store.rollback("release-notes", version=1)
    registry.scan()

    assert disabled.status == "disabled"
    assert registry.get("release-notes").description == "Draft releases"
    assert rolled_back.version == 1
    assert [event.action for event in store.events()] == [
        "draft_created",
        "promoted",
        "draft_created",
        "promoted",
        "disabled",
        "rolled_back",
    ]


# LLM: _skill_text creates a minimal SKILL.md body for lifecycle tests.
# 函数用途: 生成带 frontmatter 的 skill Markdown 内容。
def _skill_text(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
