"""LLM: focused tests for structured subagent role contracts.

函数/模块用途: 验证角色推断只依赖结构化 role / template id / tool grants，不从自然语言 goal 里猜。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.hierarchy.scheduled_role import (
    role_from_child_spec_identity,
    scheduled_child_role,
)
from agent_py_agent.agent.subagents.services.hierarchy.scheduler_models import HierarchyChildSpec


def test_role_identity_ignores_natural_language_goal():
    spec = HierarchyChildSpec(
        goal="请测试页面并验收，然后创建下一层子代理继续处理。",
        role="child",
        agent_name="小傻妞-临时助手",
    )

    assert role_from_child_spec_identity(spec) == "worker"


def test_role_identity_reads_template_ids_from_structured_identity():
    spec = HierarchyChildSpec(
        goal="检查产物。",
        role="tester",
        agent_name="小傻妞-任意显示名",
    )

    assert role_from_child_spec_identity(spec) == "tester"


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
