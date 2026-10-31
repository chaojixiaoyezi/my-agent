"""learnpack 第 8 步真模型验收发现的修正（用户 2026-10-06 定方案 A）：她把写包文件派给子代理后，子代理写完后的后台续跑要能打包、
按开关安装或开待确认安装单。

锁定：子代理整合续跑（subagent_integration）和其它后台 profile 的候选目录里都有 package_build、package_install，并带中文动作说明；
本机管理员主代理按这份目录生成的续跑工具快照里两个工具都在（第 8 步那次的快照里没有，连续 TOOL_UNAVAILABLE 后回合被停）；
续跑继承当前主链的发现入口；非管理员 owner 没注册这两个工具，目录里的名字不授予任何东西；后台授权闭包不强制前台直出，
打包工具和 skill_summarize 在前台仍然收起。
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
    # 7101c9fa2 起自我发现入口只剩 tool_search（list_tools 仅在版本化输入边界折算为它）。
    assert "tool_search" in decision.allowed_tools and "list_tools" not in decision.allowed_tools, (
        "续跑保留当前自我发现入口，目录本身仍不授权")
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


# LLM: TOOL-04区分执行授权与前台展示；后台显式上界不丢工具，前台通过原发现链恢复同一Schema。
# 函数用途: 验证打包工具保留后台授权且默认不占前台原生工具表。
def test_continuation_required_tools_remain_authorized_but_folded_in_foreground(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    assert _PACKAGE_TOOLS <= BACKGROUND_CONTINUATION_REQUIRED_TOOLS
    visible = {spec.name for spec in agent.tools.model_visible_specs()}
    assert not visible & _PACKAGE_TOOLS, "后台授权闭包不决定前台展示"
    snapshot = agent.tools.runtime_snapshot(allowed_tools=sorted(BACKGROUND_CONTINUATION_REQUIRED_TOOLS))
    assert snapshot.available_tool_names >= _PACKAGE_TOOLS
    found = agent.tools.tools["tool_search"].execute({"names": sorted(_PACKAGE_TOOLS)})
    assert found.ok
    loaded = found.result_envelope["tool_search"]["loaded_tool_names"]
    assert set(loaded) == _PACKAGE_TOOLS
    assert {spec.name for spec in agent.tools.model_visible_specs(loaded_tool_names=loaded)} >= _PACKAGE_TOOLS
    assert "skill_summarize" not in visible, "总结工具不在后台目录里，照旧默认收起"
