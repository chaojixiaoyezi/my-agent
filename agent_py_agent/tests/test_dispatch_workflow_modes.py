"""LLM: Focused dispatch workflow-mode tests split from the large dispatch suite.

模块用途: 验证 workflow plan/auto 两种调度模式，避免主 dispatch 测试文件继续变大。
"""

from __future__ import annotations

from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: test_workflow_plan_mode_persists_plan_only covers dry planning without child creation.
# 函数用途: workflow_mode=plan 时只给父任务补计划，不创建 worker child。
def test_workflow_plan_mode_persists_plan_only(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    parent = agent.subagents.create_run(
        goal="Fix API bug and add regression tests",
        thought="先建父工单，再由 dispatch 补做 workflow 规划。",
        plan=["等待规划"],
    )

    report = agent.dispatch_subagents(
        CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs()),
        CapabilityConfig(),
        apply=True,
        workflow_mode="plan",
        max_runners=0,
    )
    loaded = agent.subagents.load(parent.id)

    assert any(item.step == "workflow" and item.action == "plan_workflow" and item.ok for item in report.records)
    assert loaded.workflow_mode == "plan"
    assert loaded.workflow_template_id == "code_feature_split"
    assert loaded.workflow_plan["ok"] is True
    assert loaded.workflow_child_run_ids == []
    assert loaded.child_ids == []


# LLM: test_workflow_auto_mode_spawns_worker_children covers planned child creation.
# 函数用途: workflow_mode=auto 时把父任务计划展开成 implementation/tests 等 worker child。
def test_workflow_auto_mode_spawns_worker_children(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    parent = agent.subagents.create_run(
        goal="Fix API bug and add regression tests",
        thought="让 workflow 自动派出 implementation/tests worker。",
        plan=["等待自动派工"],
        agent_name="小傻妞-api-parent",
    )

    report = agent.dispatch_subagents(
        CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs()),
        CapabilityConfig(),
        apply=True,
        workflow_mode="auto",
        max_runners=0,
    )
    loaded = agent.subagents.load(parent.id)
    children = [agent.subagents.load(run_id) for run_id in loaded.workflow_child_run_ids]

    assert any(item.step == "workflow" and item.action == "spawn_workflow_workers" for item in report.records)
    assert loaded.workflow_mode == "auto"
    assert loaded.workflow_plan["ok"] is True
    assert len(loaded.workflow_child_run_ids) == 3
    assert len(children) == 3
    assert {child.workflow_phase_id for child in children} == {"design_contract", "implementation", "tests"}
    assert all(child.parent_id == loaded.id for child in children)
    assert all(child.agent_name.startswith("小小傻妞-") for child in children)
    assert any(child.workflow_depends_on == ["design_contract"] for child in children)
