"""LLM: tests for per-agent rolling tool-call budgets.

模块用途: 验证单个子代理的工具调用预算只按 run_id 生效，不限制主代理或整个任务树。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_agent_budget import (
    ToolAgentBudgetRequest,
    check_tool_agent_budget,
)


# LLM: _agent creates the minimum config surface used by the budget helper.
# 函数用途: 生成测试用代理对象，带 10 分钟窗口和可调最大调用次数。
def _agent(max_calls: int = 2, window_seconds: int = 600):
    return SimpleNamespace(
        config=SimpleNamespace(
            tool_agent_budget_max_calls=max_calls,
            tool_agent_budget_window_seconds=window_seconds,
        )
    )


# LLM: main-agent calls without a run id must stay unlimited by this per-agent budget.
# 函数用途: 确认普通主代理聊天没有 run_id 时不会被单代理预算误伤。
def test_tool_agent_budget_ignores_calls_without_run_id():
    agent = _agent(max_calls=1)

    first = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "", "read_file", now=10.0))
    second = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "", "read_file", now=11.0))

    assert first is None
    assert second is None


# LLM: a single subagent run is blocked after its rolling budget is exhausted.
# 函数用途: 同一个 run_id 在窗口内超过预算后，返回自检提示而不是继续执行工具。
def test_tool_agent_budget_blocks_after_per_agent_window_limit():
    agent = _agent(max_calls=2)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=10.0)) is None
    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "write_file", now=20.0)) is None
    blocked = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=30.0))

    assert blocked is not None
    assert blocked.ok is False
    assert blocked.tool == "read_file"
    assert "单个代理工具预算已达到" in blocked.output
    assert "自检" in blocked.output


# LLM: sibling subagents must not consume each other's rolling tool budgets.
# 函数用途: 证明预算按 run_id 隔离，不是整个任务树共享 10 分钟 50 次。
def test_tool_agent_budget_is_scoped_per_run_id():
    agent = _agent(max_calls=1)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-a", "read_file", now=10.0)) is None
    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-b", "read_file", now=11.0)) is None
    blocked = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-a", "read_file", now=12.0))

    assert blocked is not None
    assert "run-a" in blocked.output


# LLM: old calls outside the rolling window should not keep blocking a healthy agent.
# 函数用途: 10 分钟窗口外的历史调用会过期，避免长期任务永久背负旧次数。
def test_tool_agent_budget_prunes_calls_outside_window():
    agent = _agent(max_calls=1, window_seconds=10)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=10.0)) is None
    later = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=21.0))

    assert later is None
