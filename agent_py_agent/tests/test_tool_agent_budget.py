"""LLM: tests for per-agent rolling tool-call budgets.

模块用途: 验证单个子代理的工具调用预算只按 run_id 生效，不限制主代理或整个任务树。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_guard.agent_budget import (
    ToolAgentBudgetRequest,
    check_tool_agent_budget,
)
from agent_py_agent.agent.settings import AgentConfig


def _agent(max_calls: int = 2):
    return SimpleNamespace(config=SimpleNamespace(tool_agent_budget_max_calls=max_calls))


def test_tool_agent_budget_counts_calls_without_run_id():
    # 用户规格（2026-08-07）：额度按代理实例分桶，与 run_id 无关；
    # 无 run_id 的调用（如主代理普通聊天）同样计入该代理的窗口额度。
    agent = _agent(max_calls=1)

    first = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "", "read_file", now=10.0))
    second = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "", "read_file", now=11.0))

    assert first is None
    assert second is not None


def test_tool_agent_budget_window_is_fixed_ten_minutes():
    """参数减量第 2 批：窗口固定 600 秒，只剩次数一个旋钮。"""
    agent = _agent(max_calls=3)

    for index in range(3):
        assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=float(index))) is None
    blocked = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=4.0))

    assert blocked is not None
    assert "最近 600 秒最多 3 次工具调用" in blocked.output


def test_tool_agent_budget_is_off_by_default_and_for_blank_or_zero():
    """默认关闭：AgentConfig 留空、显式 0、以及没有这个属性的配置都不限次数（守卫文件里不再有 200 次的副本）。"""
    assert AgentConfig().tool_agent_budget_max_calls is None
    for config in (
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        SimpleNamespace(tool_agent_budget_max_calls=0),
        SimpleNamespace(),
    ):
        agent = SimpleNamespace(config=config)
        for index in range(250):
            assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=float(index))) is None


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


def test_tool_agent_budget_is_scoped_per_agent_not_run_id():
    # 用户规格：同一代理实例的多个 run 共享该代理的窗口额度（不按 run_id 分桶）
    agent = _agent(max_calls=1)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-a", "read_file", now=10.0)) is None
    blocked = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-b", "read_file", now=11.0))

    assert blocked is not None
    assert "单个代理工具预算已达到" in blocked.output


def test_tool_agent_budget_agents_do_not_share_quota():
    # 用户规格：主代理与子代理各独立额度，用户间不共用（不同 agent 实例互不影响）
    main = _agent(max_calls=1)
    subagent = _agent(max_calls=1)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(main, "run-a", "read_file", now=10.0)) is None
    assert check_tool_agent_budget(ToolAgentBudgetRequest(main, "run-b", "read_file", now=11.0)) is not None
    # 子代理是独立实例：不受主代理已用额度影响
    assert check_tool_agent_budget(ToolAgentBudgetRequest(subagent, "run-c", "read_file", now=11.0)) is None


def test_tool_agent_budget_prunes_calls_outside_window():
    agent = _agent(max_calls=1)

    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=10.0)) is None
    assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=609.0)) is not None
    later = check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=611.0))

    assert later is None
