"""LLM: Focused dispatch workflow-mode tests split from the large dispatch suite.

模块用途: 验证 workflow plan/auto 两种调度模式，避免主 dispatch 测试文件继续变大。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
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


# LLM: test_model_dispatch_run_ids_respects_task_workflow_off covers real root-created worker E2E.
# 函数用途: 模型显式推进某个 workflow=off 的 run 时，不能因为全局 workflow auto 又给它套 implement/verify 子任务。
def test_model_dispatch_run_ids_respects_task_workflow_off(tmp_path):
    config = AgentConfig(model_backend="echo", subagent_workspace="subs")
    config.subagent_workflow_mode = "auto"
    agent = SimpleAgent(config, tmp_path)
    task = agent.subagents.create_run(
        goal="创建一个高端现代家具品牌网站首页的单文件HTML",
        thought="直接完成父级交付，不自动套 workflow。",
        plan=["写页面", "交验收"],
        role="worker",
        acceptance_checks=[f"文件必须保存到 {tmp_path}/deliverables/furniture-home/index.html"],
        workflow_mode="off",
    )

    result = DispatchSubagentsTool(agent).execute({
        "apply": True,
        "execute_runners": False,
        "max_runners": 0,
        "run_ids": [task.id],
    })
    payload = json.loads(result.output)
    loaded = agent.subagents.load(task.id)

    assert result.ok is True
    assert loaded.workflow_mode == "off"
    assert loaded.workflow_child_run_ids == []
    assert all(record["step"] != "workflow" for record in payload["records"])
