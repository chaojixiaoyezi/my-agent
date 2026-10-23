# LLM: 展示投影只收紧原授权闭包；使用结构化唤醒，不识别正文意图或写入真实 owner 配置。
# 模块用途: 验证后台首轮按需展示与执行授权相互独立。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_tool_policy as policy


# LLM: 新入口不存在时明确断言失败，避免导入异常掩盖缺失行为。
# 函数用途: 调用后台首轮展示策略。
def initial(request):
    entry = getattr(policy, "background_initially_presented_tools", None)
    assert callable(entry), "缺少初次展示投影"
    return entry(request)


# LLM: 默认展示不改变授权结果；查找入口保留，重工具可以发现而非删除。
# 函数用途: 验证通用唤醒的授权与初次呈现分离。
def test_generic_background_defers_heavy_tools_without_revoking_them():
    request = policy.BackgroundToolPolicyRequest(reason="subagent_runner_finished")
    allowed = frozenset(policy.background_allowed_tools(request=request))
    presented = initial(request)
    heavy = {"watch_stream", "schedule", "package_build", "package_install", "remember", "update_persona"}
    assert heavy <= allowed
    assert not heavy.intersection(presented)
    assert "tool_search" in presented and presented <= allowed
    assert frozenset(policy.background_allowed_tools(request=request)) == allowed


# LLM: 结构化精确目标只预载被提到的能力，正文包含工具名不算目标。
# 函数用途: 验证唤醒信号的工具选择不会解析自由文本。
def test_structured_targets_preload_but_prose_does_not():
    request = policy.BackgroundToolPolicyRequest(wake_signal={"tool_names": ["package_build", "package_install"],
                                                             "message": "remember update_persona watch_stream"})
    presented = initial(request)
    assert {"package_build", "package_install"} <= presented
    assert not {"remember", "update_persona", "watch_stream"}.intersection(presented)


# LLM: 即便唤醒精确指定也不能绕过 owner 禁用项、任务白名单及主动投递边界。
# 函数用途: 验证多 owner 和任务级减法仍是唯一授权上界。
def test_targeted_preload_never_expands_owner_or_task_authorization():
    request = policy.BackgroundToolPolicyRequest(reason="audit_finding", proactive_delivery_available=False,
        owner_policy=SimpleNamespace(disabled_tools=("package_install",)),
        policy_snapshot={"allowed_tools": ["tool_search", "read_file", "package_build", "package_install", "send_message"]},
        wake_signal={"tool_names": ["package_build", "package_install", "watch_stream", "send_message", "unknown"]})
    assert initial(request) == frozenset({"tool_search", "read_file", "package_build"})


# LLM: 只使用规范 reason 及目标持久状态，不能把模型文字当作目标或子代理事实。
# 函数用途: 验证有状态唤醒保留相应控制入口。
@pytest.mark.parametrize(("wake_request", "expected"), [
    (policy.BackgroundToolPolicyRequest(reason="audit_finding"), {"watch_stream"}),
    (policy.BackgroundToolPolicyRequest(reason="scheduled_job_due"), {"schedule"}),
    (policy.BackgroundToolPolicyRequest(reason="thread_goal_continue", active_goal=True,
                                        goal_subagent_phase="subagents_active"),
     {"get_goal", "update_goal", "list_agents", "send_guidance"}),
])
def test_wake_facts_keep_relevant_controls(wake_request, expected):
    assert expected <= initial(wake_request)


# LLM: 缺少搜索入口时不能制造一组找不回来的工具；展示全部原允许工具。
# 函数用途: 验证窄白名单的安全回退。
def test_configured_leaf_without_search_presents_all_allowed_tools():
    request = policy.BackgroundToolPolicyRequest(config=SimpleNamespace(
        background_main_agent_allowed_tools=["read_file", "package_build"]))
    assert initial(request) == frozenset({"read_file", "package_build"})
