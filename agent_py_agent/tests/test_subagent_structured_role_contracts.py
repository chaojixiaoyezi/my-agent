"""LLM: focused tests for structured subagent role contracts.

函数/模块用途: 验证角色推断只依赖结构化 role / template id / tool grants，不从自然语言 goal 里猜。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.hierarchy_role_identity import (
    role_from_child_spec_identity,
)
from agent_py_agent.agent.subagents.services.hierarchy_scheduled_role import scheduled_child_role
from agent_py_agent.agent.subagents.services.hierarchy_scheduler_models import HierarchyChildSpec


# LLM: placeholder roles should not be repaired from natural-language goals.
# 函数用途: role=child/general 时，即使 goal 里写了“测试/验收/创建下级”，代码层也不能猜成 tester/coordinator。
def test_role_identity_ignores_natural_language_goal():
    spec = HierarchyChildSpec(
        goal="请测试页面并验收，然后创建下一层子代理继续处理。",
        role="child",
        agent_name="小傻妞-临时助手",
    )

    assert role_from_child_spec_identity(spec) == "worker"


# LLM: template ids embedded in explicit role/name fields are still protocol-level identity.
# 函数用途: LLM 已经在 role 或 agent_name 写出 tester 这类模板 id 时，可以恢复为对应角色。
def test_role_identity_reads_template_ids_from_structured_identity():
    spec = HierarchyChildSpec(
        goal="检查产物。",
        role="child",
        agent_name="小傻妞-tester-1",
    )

    assert role_from_child_spec_identity(spec) == "tester"


# LLM: coordinator inference should come from tools/role, not natural-language child-creation prose.
# 函数用途: goal 里自然语言说“创建下级”不会自动变 coordinator；显式调度工具才会。
def test_scheduled_child_role_uses_tool_grants_not_goal_keywords():
    parent = SubAgentTask(id="root", goal="root", thought="", plan=[], role="coordinator")
    natural_spec = HierarchyChildSpec(
        goal="创建 worker 和下一层子代理继续做。",
        role="worker",
        agent_name="小傻妞-worker-1",
    )
    coordinator_spec = HierarchyChildSpec(
        goal="安排下一层。",
        role="worker",
        agent_name="小傻妞-worker-2",
        allowed_tools=["schedule_child_subagents"],
    )

    assert scheduled_child_role(parent, natural_spec, goal=natural_spec.goal) == "worker"
    assert scheduled_child_role(parent, coordinator_spec, goal=coordinator_spec.goal) == "child_coordinator"
