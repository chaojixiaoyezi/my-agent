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


# LLM: test_create_run_applies_reporter_contract adds evidence expectations without changing execution.
# 函数用途: 创建 reporter 任务时补稳定验收要求，要求报告结论引用证据或 artifact。
def test_create_run_applies_reporter_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="collect evidence", thought="report only", plan=["read", "report"], role="analyst")

    assert task.role == REPORTER_ROLE
    assert any("evidence_refs" in check for check in task.acceptance_checks)
    assert task.quality_contract.cannot_self_accept is True
    assert task.quality_contract.parent_final_gate is True


# LLM: test_create_run_applies_checker_contract keeps checker read-only and parent-gated by default.
# 函数用途: 创建 checker 任务时自动补只读工具边界和不能自验收的质量合同。
def test_create_run_applies_checker_contract(tmp_path):
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(goal="verify evidence", thought="check only", plan=["inspect"], role="reviewer")

    assert task.role == CHECKER_ROLE
    assert task.allowed_tools == ["list_files", "read_file", "search_text", "read_artifact"]
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
    assert checker.allowed_tools == ["list_files", "read_file", "search_text", "read_artifact"]
