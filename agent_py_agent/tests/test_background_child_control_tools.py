"""后台子代理整合档与 goal 子代理档的直属下级管理面（2026-09-28，T3 验收观察 2 的 C）。

背景：直属下级管理面 DIRECT_CHILD_CONTROL_TOOLS 共 5 个（创建、只读状态、插话、取消、权限答复）。后台默认目录是手写的，
08-30 新增的 list_agents 没跟进，子代理生命周期唤醒片只拿到其中 4 个。
锁定：
- 整合档与 goal 子代理档在策略收紧前包含这份唯一定义里的全部工具，原目录顺序不变，只在末尾补缺；
- owner 禁用、任务白名单和显式配置仍只做减法，补进来的工具同样能被去掉，名单外的名字不会被加进来；
- 真实后台 RunParams 与注册表快照里，生命周期唤醒片确实能用 list_agents，每个工具都有给模型的用途说明。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_tool_policy import (
    CONTROL_ACTION_DESCRIPTIONS,
    DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    GOAL_BACKGROUND_ALLOWED_TOOLS,
    GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS,
    GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS,
    SUBAGENT_INTEGRATION_ALLOWED_TOOLS,
    BackgroundToolPolicyRequest,
    background_control_action_lines,
    background_tool_policy_decision,
)
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.role_templates import DIRECT_CHILD_CONTROL_TOOLS

_CHILD_WAKE = "subagent_runner_finished"
_CHILD_FACING_REQUESTS = {
    "subagent_integration": BackgroundToolPolicyRequest(reason=_CHILD_WAKE),
    "thread_goal_subagents_active": BackgroundToolPolicyRequest(
        reason="thread_goal_continue", active_goal=True, goal_subagent_phase="subagents_active",
    ),
    "thread_goal_subagents_terminal": BackgroundToolPolicyRequest(
        reason=_CHILD_WAKE, active_goal=True, goal_subagent_phase="subagents_terminal",
    ),
}


@pytest.mark.parametrize("profile", sorted(_CHILD_FACING_REQUESTS))
def test_child_facing_profiles_carry_every_direct_child_control(profile) -> None:
    decision = background_tool_policy_decision(request=_CHILD_FACING_REQUESTS[profile])

    assert decision.profile == profile
    assert set(DIRECT_CHILD_CONTROL_TOOLS) <= set(decision.allowed_tools)
    assert len(decision.allowed_tools) == len(set(decision.allowed_tools))
    # 每个可见工具都有给模型的用途说明；list_agents 的说明写明只读、不等待。
    assert [tool for tool in decision.allowed_tools if tool not in CONTROL_ACTION_DESCRIPTIONS] == []
    lines = background_control_action_lines(request=_CHILD_FACING_REQUESTS[profile])
    assert f"- list_agents: {CONTROL_ACTION_DESCRIPTIONS['list_agents']}" in lines


@pytest.mark.parametrize(("profile", "base"), [
    (SUBAGENT_INTEGRATION_ALLOWED_TOOLS, DEFAULT_BACKGROUND_ALLOWED_TOOLS),
    (GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS, GOAL_BACKGROUND_ALLOWED_TOOLS),
    (GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS, GOAL_BACKGROUND_ALLOWED_TOOLS),
])
def test_profiles_keep_their_order_and_only_append_missing_controls(profile, base) -> None:
    assert profile[:len(base)] == base
    assert set(profile[len(base):]) == set(DIRECT_CHILD_CONTROL_TOOLS) - set(base)
    assert "list_agents" in profile[len(base):]


def test_policy_tightening_still_only_subtracts() -> None:
    owner_disabled = background_tool_policy_decision(request=BackgroundToolPolicyRequest(
        reason=_CHILD_WAKE, owner_policy=SimpleNamespace(disabled_tools=["list_agents"]),
    ))
    assert "list_agents" not in owner_disabled.allowed_tools
    assert owner_disabled.removed_tools == ("list_agents",)

    task_narrowed = background_tool_policy_decision(request=BackgroundToolPolicyRequest(
        reason=_CHILD_WAKE, policy_snapshot={"allowed_tools": ["list_agents", "read_file", "not_in_profile"]},
    ))
    assert task_narrowed.allowed_tools == ("read_file", "list_agents")

    configured = SimpleNamespace(background_main_agent_allowed_tools=["read_file"])
    exact = background_tool_policy_decision(request=BackgroundToolPolicyRequest(reason=_CHILD_WAKE, config=configured))
    assert (exact.profile, exact.allowed_tools) == ("configured", ("read_file",))


def test_lifecycle_wake_run_can_use_list_agents(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), execution_mode="local_unmanaged"),
        tmp_path / "service-root",
    )
    params = _run_params(
        "thread-1", BackgroundRunRequest(thread_id="thread-1", task_id="task-1", reason=_CHILD_WAKE), agent,
    )
    snapshot = agent.tools.runtime_snapshot(allowed_tools=params.allowed_tools, run_id=params.run_id)

    assert params.allowed_tools is not None and "list_agents" in params.allowed_tools
    assert "list_agents" in snapshot.available_tool_names
