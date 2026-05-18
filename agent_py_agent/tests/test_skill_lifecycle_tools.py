from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.capability.skills import SkillLifecycleStore, SkillRegistry
from agent_py_agent.agent.tooling.capability_catalog import CapabilitySearchTool
from agent_py_agent.agent.tooling.skill_lifecycle_tools import (
    SkillDraftFromTaskTool,
    SkillLifecycleTool,
)


def test_skill_lifecycle_tool_exposes_draft_promote_disable_and_rollback(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    tool = SkillLifecycleTool(store)

    draft = _payload(
        tool.execute(
            {
                "action": "create_draft",
                "name": "api-debug",
                "markdown": _skill_text("api-debug", "Debug flaky API calls", "v1"),
                "reason": "learned from task",
            }
        )
    )
    promoted = _payload(tool.execute({"action": "promote", "name": "api-debug"}))
    disabled = _payload(tool.execute({"action": "disable", "name": "api-debug", "reason": "too broad"}))
    rolled_back = _payload(tool.execute({"action": "rollback", "name": "api-debug", "version": 1}))
    events = _payload(tool.execute({"action": "events", "name": "api-debug"}))

    assert draft["result"]["status"] == "draft"
    assert promoted["result"]["status"] == "active"
    assert disabled["result"]["status"] == "disabled"
    assert rolled_back["result"]["version"] == 1
    assert [event["action"] for event in events["events"]] == [
        "draft_created",
        "promoted",
        "disabled",
        "rolled_back",
    ]


def test_skill_draft_from_task_creates_reviewable_draft_without_activating_it(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    tool = SkillDraftFromTaskTool(store)
    registry = SkillRegistry([store.active_dir])

    result = _payload(
        tool.execute(
            {
                "request": {
                    "name": "pytest-fixture-debug",
                    "task_summary": "Diagnose pytest fixture ordering failures",
                    "when_to_use": "Use after pytest reports missing or stale fixtures",
                    "steps": ["Run the focused pytest target", "Inspect fixture dependency order"],
                    "tools_required": ["read_file", "controlled_exec"],
                    "tags": ["pytest", "fixtures"],
                    "reason": "accepted debugging pattern",
                }
            }
        )
    )
    registry.scan()

    assert result["result"]["status"] == "draft"
    assert registry.get("pytest-fixture-debug") is None
    assert "Diagnose pytest fixture ordering failures" in (store.drafts_dir / "pytest-fixture-debug" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_active_lifecycle_skills_are_visible_to_capability_catalog(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    SkillLifecycleTool(store).execute(
        {
            "action": "create_draft",
            "name": "release-check",
            "markdown": _skill_text("release-check", "Check release readiness", "body"),
        }
    )
    SkillLifecycleTool(store).execute({"action": "promote", "name": "release-check"})
    registry = SkillRegistry([store.active_dir])
    registry.scan()
    catalog = CapabilitySearchTool(tool_specs=[], skill_registry=registry)

    payload = _payload(catalog.execute({"query": "release readiness", "kind": "skill"}))

    assert [item["id"] for item in payload["results"]] == ["skill:release-check"]


def test_capability_catalog_refreshes_skill_registry_after_runtime_promote(tmp_path: Path) -> None:
    store = SkillLifecycleStore(tmp_path / "skills")
    registry = SkillRegistry([store.active_dir])
    registry.scan()
    catalog = CapabilitySearchTool(tool_specs=[], skill_registry=registry)

    SkillLifecycleTool(store).execute(
        {
            "action": "create_draft",
            "name": "runtime-promote",
            "markdown": _skill_text("runtime-promote", "Runtime promoted skill", "body"),
        }
    )
    SkillLifecycleTool(store).execute({"action": "promote", "name": "runtime-promote"})

    payload = _payload(catalog.execute({"query": "runtime promoted", "kind": "skill"}))

    assert [item["id"] for item in payload["results"]] == ["skill:runtime-promote"]


def _payload(result):
    assert result.ok is True
    return json.loads(result.output)


def _skill_text(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
