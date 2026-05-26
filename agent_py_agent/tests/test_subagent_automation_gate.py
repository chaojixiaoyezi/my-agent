"""LLM: focused tests for semi-auto to auto subagent execution gates.

函数/模块用途: 验证子代理自动化只放行 refs-only 安全动作，跑工具/改状态仍需要人工确认。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.automation_gate import (
    SubAgentAutomationGateRequest,
    evaluate_subagent_automation_gate,
)


# LLM: test_auto_gate_allows_refs_only_recovery_query defines the first automatic-safe action.
# 函数用途: 自动模式下，只读恢复树查询有 refs 且不改状态时可以放行。
def test_auto_gate_allows_refs_only_recovery_query():
    result = evaluate_subagent_automation_gate(
        SubAgentAutomationGateRequest(
            mode="auto",
            action="query_recovery_tree",
            refs=["reports/takeover_readiness.json"],
            mutates_task_state=False,
            executes_tools=False,
        )
    )

    assert result.execution_allowed is True
    assert result.automatic_execution_allowed is True
    assert result.guard_status == "allowed"
    assert result.blocked_by == []


# LLM: test_auto_gate_blocks_mutating_action_without_manual_confirmation protects state changes.
# 函数用途: 自动模式下，任何会改 task 状态的动作都不能直接放行。
def test_auto_gate_blocks_mutating_action_without_manual_confirmation():
    result = evaluate_subagent_automation_gate(
        SubAgentAutomationGateRequest(
            mode="auto",
            action="apply_followup",
            refs=["reports/final_closeout_note.json"],
            mutates_task_state=True,
            executes_tools=False,
        )
    )

    assert result.execution_allowed is False
    assert result.automatic_execution_allowed is False
    assert result.guard_status == "blocked"
    assert "mutates_task_state" in result.blocked_by
    assert result.recommended_next_step == "request_manual_confirmation"


# LLM: test_manual_confirmed_tool_action_is_semi_auto_not_automatic keeps test execution bounded.
# 函数用途: 人工确认后可执行半自动工具动作，但它仍不算无人值守自动放行。
def test_manual_confirmed_tool_action_is_semi_auto_not_automatic():
    result = evaluate_subagent_automation_gate(
        SubAgentAutomationGateRequest(
            mode="semi_auto",
            action="execute_parent_tests",
            refs=["reports/final_closeout_note.json"],
            mutates_task_state=False,
            executes_tools=True,
            manual_confirmed=True,
        )
    )

    assert result.execution_allowed is True
    assert result.automatic_execution_allowed is False
    assert result.guard_status == "manual_confirmed"
    assert result.recommended_next_step == "execute_with_audit"


# LLM: test_auto_gate_blocks_refs_only_action_when_recovery_batch_is_too_large prevents runaway loops.
# 函数用途: 即使是只读动作，候选恢复节点过多也会阻断，避免无人值守扩大上下文和循环。
def test_auto_gate_blocks_refs_only_action_when_recovery_batch_is_too_large():
    result = evaluate_subagent_automation_gate(
        SubAgentAutomationGateRequest(
            mode="auto",
            action="query_recovery_tree",
            refs=["reports/recovery.json"],
            recovery_candidate_count=8,
            max_recovery_candidates=3,
        )
    )

    assert result.execution_allowed is False
    assert result.automatic_execution_allowed is False
    assert "too_many_recovery_candidates" in result.blocked_by
