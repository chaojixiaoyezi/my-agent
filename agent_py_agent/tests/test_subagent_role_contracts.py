"""LLM: focused tests for stable reporter/checker role contracts.

函数/模块用途: 验证通用子代理角色不再只是自由字符串，同时旧 analyst/reviewer 不会被隐式改写。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_contracts import (
    CHECKER_ROLE,
    REPORTER_ROLE,
    normalize_subagent_role,
)
from agent_py_agent.agent.subagents.role_templates import role_template_id_for_role
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_role_normalization_does_not_map_legacy_names():
    assert normalize_subagent_role("analyst") == "analyst"
    assert normalize_subagent_role("reviewer") == "reviewer"
    assert normalize_subagent_role("reporter") == REPORTER_ROLE
    assert normalize_subagent_role("checker") == CHECKER_ROLE
    assert normalize_subagent_role("custom-reviewer") == "custom-reviewer"


def test_structured_hierarchy_role_ids_do_not_use_legacy_aliases():
    assert role_template_id_for_role("coordinator") == "coordinator"
    assert role_template_id_for_role("worker") == "worker"
    assert role_template_id_for_role("coordinator_alias") == ""
    assert role_template_id_for_role("worker_alias") == ""
    assert role_template_id_for_role("qa_tester") == ""


def test_create_run_uses_template_defaults_for_explicit_role(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="继续拆分示例网站条目目录任务",
        thought="coordinate",
        plan=["split", "dispatch"],
        role="coordinator",
    )

    assert task.role == "coordinator"
    assert "create_subagents" in task.allowed_tools
    assert "send_guidance" in task.allowed_tools
    assert task.acceptance_checks == []


def test_unknown_llm_role_keeps_base_tools_without_template_fallback(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="实现示例网站页脚",
        thought="build",
        plan=["implement", "report"],
        role="frontend_footer_builder",
    )

    assert task.role == "frontend_footer_builder"
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert task.acceptance_checks == []


def test_explicit_leaf_grant_cannot_retain_direct_child_controls(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="实现页面",
        thought="build",
        plan=["implement"],
        role="worker",
        allowed_tools=[
            "read_file",
            "create_subagents",
            "send_guidance",
            "cancel_subagents",
            "resolve_capability_requests",
            "capability_request",
        ],
    )

    assert task.allowed_tools == ["read_file", "capability_request"]


def test_create_run_applies_reporter_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="collect evidence", thought="report only", plan=["read", "report"], role="reporter")

    assert task.role == REPORTER_ROLE
    assert task.acceptance_checks == []


def test_create_run_applies_checker_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="verify evidence", thought="check only", plan=["inspect"], role="checker")

    assert task.role == CHECKER_ROLE
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "apply_patch" in task.allowed_tools
    assert task.acceptance_checks == []


def test_hierarchy_scheduler_applies_role_contracts_to_children(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="root", thought="orchestrate", plan=["split"])

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="write report", role="reporter", agent_name="reporter-a"),
                HierarchyChildSpec(goal="check report", role="checker", agent_name="checker-a"),
            ],
        )
    )

    reporter = manager.load(result.created_run_ids[0])
    checker = manager.load(result.created_run_ids[1])
    assert reporter.role == REPORTER_ROLE
    assert checker.role == CHECKER_ROLE
    assert "read_file" in checker.allowed_tools
    assert "write_file" in checker.allowed_tools
    assert "apply_patch" in checker.allowed_tools
