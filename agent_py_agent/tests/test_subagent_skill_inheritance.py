from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.hierarchy.scheduler_models import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _agent_with_skills(tmp_path: Path) -> tuple[SimpleAgent, Path]:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "one", "first delegated skill", "ONE BODY")
    _write_skill(skill_root, "two", "second private skill", "TWO BODY")
    agent = SimpleAgent(
        AgentConfig(
            enable_subagents=True,
            subagent_workspace="subs",
            prompt_files=[],
        ),
        tmp_path / "repo",
    )
    agent.skills_service.set_extra_roots([skill_root])
    return agent, skill_root


def test_create_subagent_pins_requested_skill_snapshot(tmp_path: Path) -> None:
    agent, _ = _agent_with_skills(tmp_path)
    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "使用指定技能完成只读检查",
            "allowed_skills": ["one"],
            "tool_preset": "read_only",
            "defer_start": True,
        }
    )
    assert result.ok is True
    task = agent.subagents.list_runs()[0]
    assert task.allowed_skills == ["workspace:one"]
    refs = task.attributes["skill_snapshot_refs"]
    assert refs[0]["stable_id"] == "workspace:one"
    assert len(refs[0]["content_sha256"]) == 64
    assert "path" not in refs[0]


def test_create_subagents_rejects_unknown_skill_without_partial_create(tmp_path: Path) -> None:
    agent, _ = _agent_with_skills(tmp_path)
    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "批量工作",
            "items": [
                {"goal": "有效项", "allowed_skills": ["one"]},
                {"goal": "无效项", "allowed_skills": ["missing"]},
            ],
            "defer_start": True,
        }
    )
    assert result.ok is False
    assert "items[1]" in result.output
    assert agent.subagents.list_runs() == []


def test_runner_skill_tool_sees_only_explicit_snapshot_subset(tmp_path: Path) -> None:
    agent, _ = _agent_with_skills(tmp_path)
    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "只使用技能一",
            "allowed_skills": ["one"],
            "defer_start": True,
        }
    )
    assert result.ok
    task = agent.subagents.list_runs()[0]
    previous = set_current_subagent_context(
        agent,
        run_id=task.id,
        task_attributes=task.attributes,
    )
    try:
        allowed = SkillSearchTool(agent).execute(
            {"action": "get", "skill_id": "workspace:one"}
        )
        denied = SkillSearchTool(agent).execute(
            {"action": "get", "skill_id": "workspace:two"}
        )
    finally:
        restore_current_subagent_context(agent, previous)
    assert allowed.ok and "ONE BODY" in allowed.output
    assert denied.ok is False
    assert "当前轮没有这个可用 skill_id" in denied.output


def test_runner_skill_snapshot_fails_closed_after_source_changes(tmp_path: Path) -> None:
    agent, skill_root = _agent_with_skills(tmp_path)
    result = CreateSubagentsTool(agent).execute(
        {"goal": "使用技能一", "allowed_skills": ["one"], "defer_start": True}
    )
    assert result.ok
    task = agent.subagents.list_runs()[0]
    _write_skill(skill_root, "one", "changed after delegation", "CHANGED BODY")
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)
    try:
        read = SkillSearchTool(agent).execute(
            {"action": "get", "skill_id": "workspace:one"}
        )
    finally:
        restore_current_subagent_context(agent, previous)
    assert read.ok is False
    assert read.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert "SKILL_SNAPSHOT_STALE" in read.output


def test_descendant_cannot_expand_parent_skill_scope_and_inherits_hashes(tmp_path: Path) -> None:
    agent, _ = _agent_with_skills(tmp_path)
    result = CreateSubagentsTool(agent).execute(
        {"goal": "父任务", "allowed_skills": ["one"], "defer_start": True}
    )
    assert result.ok
    parent = agent.subagents.list_runs()[0]

    denied = agent.subagents.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="越权子任务", allowed_skills=["workspace:two"])],
            apply=True,
        )
    )
    assert denied.blocked is True
    assert denied.reason == "skill_scope_expansion_denied:workspace:two"

    created = agent.subagents.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="合法子任务", allowed_skills=["workspace:one"])],
            apply=True,
        )
    )
    assert created.blocked is False
    child = agent.subagents.load(created.created_run_ids[0])
    assert child.allowed_skills == ["workspace:one"]
    assert child.attributes["skill_snapshot_refs"] == parent.attributes["skill_snapshot_refs"]
