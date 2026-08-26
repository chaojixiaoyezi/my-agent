"""LLM: focused tests for orchestration tool constant sets.

模块用途: 验证子代理编排工具常量保持基础读写能力，避免主测试文件继续膨胀。
"""

from __future__ import annotations

from pathlib import Path


def test_read_only_subagent_tools_contains_read_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import READ_ONLY_SUBAGENT_TOOLS

    assert "list_files" in READ_ONLY_SUBAGENT_TOOLS
    assert "read_file" in READ_ONLY_SUBAGENT_TOOLS
    assert "search_text" in READ_ONLY_SUBAGENT_TOOLS
    assert "write_file" not in READ_ONLY_SUBAGENT_TOOLS
    assert "apply_patch" not in READ_ONLY_SUBAGENT_TOOLS
    assert "run_command" not in READ_ONLY_SUBAGENT_TOOLS


def test_coding_subagent_tools_defaults_to_leaf_execution_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS

    assert "write_file" in CODING_SUBAGENT_TOOLS
    assert "edit_file" in CODING_SUBAGENT_TOOLS
    assert "apply_patch" in CODING_SUBAGENT_TOOLS
    assert "read_file" in CODING_SUBAGENT_TOOLS
    assert "list_files" in CODING_SUBAGENT_TOOLS
    assert "process_session" in CODING_SUBAGENT_TOOLS
    assert "create_subagents" not in CODING_SUBAGENT_TOOLS
    assert "send_guidance" not in CODING_SUBAGENT_TOOLS
    assert "cancel_subagents" not in CODING_SUBAGENT_TOOLS
    assert "resolve_capability_requests" not in CODING_SUBAGENT_TOOLS
    assert "raise_event" not in CODING_SUBAGENT_TOOLS
    assert "schedule_child_subagents" not in CODING_SUBAGENT_TOOLS
    assert "dispatch_subagents" not in CODING_SUBAGENT_TOOLS
    assert "inspect_agent_tree" not in CODING_SUBAGENT_TOOLS


def test_retired_subagent_controls_are_removed_from_legacy_snapshots():
    from agent_py_agent.agent.subagents.role_templates import (
        active_model_subagent_tools,
    )

    assert active_model_subagent_tools(
        [
            "read_file",
            "inspect_agent_tree",
            "dispatch_subagents",
            "schedule_child_subagents",
            "raise_event",
            "send_guidance",
        ]
    ) == ["read_file", "send_guidance"]


def test_background_shell_companion_is_restored_in_legacy_snapshots():
    from agent_py_agent.agent.subagents.role_templates import (
        active_model_subagent_tools,
    )

    assert active_model_subagent_tools(["read_file", "run_command"]) == [
        "read_file",
        "run_command",
        "process_session",
    ]


def test_raise_event_is_not_registered_as_a_model_tool(tmp_path):
    """进展和结束由宿主事件负责，模型工具目录不能再暴露旧上报入口。"""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    registered = {spec.name for spec in agent.tools.specs(include_orchestration=True)}

    assert "raise_event" not in registered


def test_builtin_skills_do_not_teach_retired_subagent_controls():
    """活跃 Skill 不能继续教模型调用已经从工具箱删除的旧入口。"""
    from agent_py_agent.agent.subagents.role_templates import (
        RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS,
    )

    skills_root = Path(__file__).parents[1] / "skills" / "builtin"
    stale: dict[str, list[str]] = {}
    for skill_path in sorted(skills_root.rglob("SKILL.md")):
        text = skill_path.read_text(encoding="utf-8")
        names = sorted(name for name in RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS if name in text)
        if names:
            stale[str(skill_path.relative_to(skills_root))] = names

    assert stale == {}
