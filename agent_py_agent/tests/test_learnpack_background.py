"""learnpack 第 8 步真模型验收发现的修正（用户 2026-10-06 定方案 A）：她把写包文件派给子代理后，子代理写完后的后台续跑要能打包、
按开关安装或开待确认安装单。

锁定：子代理整合续跑（subagent_integration）和其它后台 profile 的候选目录里都有 package_build、package_install，并带中文动作说明；
本机管理员主代理按这份目录生成的续跑工具快照里两个工具都在（第 8 步那次的快照里没有，连续 TOOL_UNAVAILABLE 后回合被停）；
续跑仍然没有 tool_search；非管理员 owner 没注册这两个工具，目录里的名字不授予任何东西；进了续跑必需集合的工具不再默认收起
（已知代价），skill_summarize 仍然收起。
"""
from __future__ import annotations

from agent_py_agent.agent.conversation.background_tool_policy import (
    BACKGROUND_CONTINUATION_REQUIRED_TOOLS,
    BackgroundToolPolicyRequest,
    background_control_action_lines,
    background_tool_policy_decision,
)
from agent_py_agent.agent.conversation.models import SESSION_TASK_WAKE_REASON
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig

_PACKAGE_TOOLS = {"package_build", "package_install"}


def _agent(tmp_path, monkeypatch, *, owner_kind="main", owner_id="main") -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, enable_subagents=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id), tmp_path / "project")


def test_the_subagent_integration_continuation_can_build_and_install(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    request = BackgroundToolPolicyRequest(reason="subagent_runner_finished", config=agent.config)
    decision = background_tool_policy_decision(agent.config, request=request)
    assert decision.profile == "subagent_integration" and set(decision.allowed_tools) >= _PACKAGE_TOOLS
    assert "tool_search" not in decision.allowed_tools, "后台续跑照旧不搜工具，所以要直接列进目录"
    snapshot = agent.tools.runtime_snapshot(allowed_tools=list(decision.allowed_tools))
    assert all(snapshot.runtime(name) is not None for name in _PACKAGE_TOOLS), "续跑工具快照里两个工具都在"
    lines = background_control_action_lines(agent.config, request=request)
    assert any(line.startswith("- package_install: ") and "确认行" in line for line in lines)
    assert any(line.startswith("- package_build: ") for line in lines)


def test_every_background_profile_offers_them_but_other_owners_get_nothing(tmp_path, monkeypatch):
    for reason in ("", "thread_goal_continue", "audit_finding", SESSION_TASK_WAKE_REASON, "subagent_capability_granted"):
        decision = background_tool_policy_decision(request=BackgroundToolPolicyRequest(reason=reason))
        assert set(decision.allowed_tools) >= _PACKAGE_TOOLS, reason
    user = _agent(tmp_path / "u", monkeypatch, owner_kind="user", owner_id="alice")
    decision = background_tool_policy_decision(user.config, request=BackgroundToolPolicyRequest(reason="subagent_runner_finished"))
    snapshot = user.tools.runtime_snapshot(allowed_tools=list(decision.allowed_tools))
    assert all(snapshot.runtime(name) is None for name in _PACKAGE_TOOLS), "目录里的名字不授权，非管理员照旧没有"


def test_continuation_required_tools_are_no_longer_folded(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    assert _PACKAGE_TOOLS <= BACKGROUND_CONTINUATION_REQUIRED_TOOLS
    visible = {spec.name for spec in agent.tools.model_visible_specs()}
    assert visible >= _PACKAGE_TOOLS, "进了续跑必需集合就始终直出（用户定的代价）"
    assert "skill_summarize" not in visible, "总结工具不在后台目录里，照旧默认收起"
