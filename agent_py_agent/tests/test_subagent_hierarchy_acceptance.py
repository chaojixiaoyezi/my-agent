"""LLM: regression tests for hierarchy acceptance fallback derivation.

函数/模块用途: 验证模型漏写 acceptance_checks 时，scheduler 仍会派生足够的验收事实给 Context Gate。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: This regression mirrors real E2E leaf blocking when a coordinator forgot acceptance_checks.
# 函数用途: 确认层级调度能从自包含 goal 派生最小验收项，避免叶子节点因模型漏传字段而直接 BLOCKED。
def test_hierarchy_schedule_derives_acceptance_checks_when_model_omits_them(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal=f"grandchild coordinator delegates proof writing under {deliverables}",
        thought="delegate to leaf only",
        plan=["create leaf", "verify result"],
        role="grandchild_coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/proof.txt，内容必须是 context-lineage-ok。",
                    agent_name="leaf-writer",
                    role="leaf_worker",
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])
    context = manager.write_execution_context(leaf.id)

    assert leaf.acceptance_checks
    assert any("proof.txt" in item for item in leaf.acceptance_checks)
    assert context.context_bundle["gate"]["ok"] is True
    assert context.context_bundle["gate"]["missing_fields"] == []
