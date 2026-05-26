"""LLM: focused tests for stable reporter/checker role contracts.

函数/模块用途: 验证通用子代理角色不再只是自由字符串，旧 analyst/reviewer 也能映射到新角色边界。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_contracts import (
    CHECKER_ROLE,
    REPORTER_ROLE,
    normalize_subagent_role,
)
from agent_py_agent.agent.subagents.role_templates import role_template_id_for_role
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_role_aliases_normalize_legacy_names protects compatibility with existing log-analysis roles.
# 函数用途: 确认 analyst/reviewer 旧角色名可继续使用，但内部会映射到 reporter/checker。
def test_role_aliases_normalize_legacy_names():
    assert normalize_subagent_role("analyst") == REPORTER_ROLE
    assert normalize_subagent_role("reviewer") == CHECKER_ROLE
    assert normalize_subagent_role("reporter") == REPORTER_ROLE
    assert normalize_subagent_role("checker") == CHECKER_ROLE
    assert normalize_subagent_role("custom-reviewer") == "custom-reviewer"


# LLM: test_natural_hierarchy_role_names_resolve_to_template_ids protects template-driven dispatch.
# 函数用途: LLM 写出 child_coordinator / leaf_worker / qa_tester 这类自然角色名时，系统要找到模板而不是给空工具。
def test_natural_hierarchy_role_names_resolve_to_template_ids():
    assert role_template_id_for_role("child_coordinator") == "coordinator"
    assert role_template_id_for_role("grandchild-coordinator") == "coordinator"
    assert role_template_id_for_role("leaf_worker") == "worker"
    assert role_template_id_for_role("qa_tester") == "tester"


# LLM: test_create_run_uses_template_defaults_after_natural_role_resolution covers the R80 regression.
# 函数用途: 创建 child_coordinator 时必须套协调模板并获得派下级工具，不能落成无工具的自由角色。
def test_create_run_uses_template_defaults_after_natural_role_resolution(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="继续拆分示例网站条目目录任务",
        thought="coordinate",
        plan=["split", "dispatch"],
        role="child_coordinator",
    )

    assert task.role == "child_coordinator"
    assert "schedule_child_subagents" in task.allowed_tools
    assert "subagent_message" in task.allowed_tools
    assert any("协调子代理" in check for check in task.acceptance_checks)


# LLM: test_unknown_llm_role_falls_back_to_worker_template avoids zero-tool agents.
# 函数用途: LLM 造出非模板 role 时，任务树可保留该名字，但执行能力至少按 worker 模板补齐。
def test_unknown_llm_role_falls_back_to_worker_template(tmp_path):
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
    assert any("执行子代理" in check for check in task.acceptance_checks)


# LLM: test_create_run_applies_reporter_contract adds evidence expectations without changing execution.
# 函数用途: 创建 reporter 任务时补稳定验收要求，要求报告结论引用证据或 artifact。
def test_create_run_applies_reporter_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="collect evidence", thought="report only", plan=["read", "report"], role="analyst")

    assert task.role == REPORTER_ROLE
    assert any("evidence_refs" in check for check in task.acceptance_checks)
    assert task.quality_contract.cannot_self_accept is True
    assert task.quality_contract.parent_final_gate is True


# LLM: test_create_run_applies_checker_contract keeps checker capable and parent-gated by default.
# 函数用途: 创建 checker 任务时保留基础读写/报告能力，但最终验收仍由父级门决定。
def test_create_run_applies_checker_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="verify evidence", thought="check only", plan=["inspect"], role="reviewer")

    assert task.role == CHECKER_ROLE
    assert "read_file" in task.allowed_tools
    assert "write_file" in task.allowed_tools
    assert "apply_patch" in task.allowed_tools
    assert task.quality_contract.final_judge == "parent_final_gate"
    assert task.quality_contract.cannot_self_accept is True
    assert task.quality_contract.parent_final_gate is True
    assert any("cannot self-accept" in check for check in task.acceptance_checks)


# LLM: test_hierarchy_scheduler_applies_role_contracts_to_children covers nested creation.
# 函数用途: 层级调度创建 child 时同样套用 reporter/checker 角色契约。
def test_hierarchy_scheduler_applies_role_contracts_to_children(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="root", thought="orchestrate", plan=["split"])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="write report", role="analyst", agent_name="analyst-a"),
                HierarchyChildSpec(goal="check report", role="reviewer", agent_name="reviewer-a"),
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
