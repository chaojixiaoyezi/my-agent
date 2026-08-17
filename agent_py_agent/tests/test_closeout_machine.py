"""模块用途: 收口状态机 decide_closeout 的 truth table 穷举测试（四改之 2
步骤 1 验收）。用独立的 oracle 复刻优先级规则, 全组合扫描比对实现输出,
另加显式场景测试锚定 EXEC-39/EXEC-30/预算/轮数语义。改动实现前先看
docs/design/closeout_state_machine.md §3/§4。
"""
from __future__ import annotations

from itertools import product

import pytest

from agent_py_agent.agent.conversation.closeout import (
    CloseoutFacts,
    CloseoutOutcome,
    decide_closeout,
)


def _oracle(facts: CloseoutFacts) -> CloseoutOutcome:
    """独立复刻优先级规则（与实现分开写，防同错共真）。"""
    status = str(facts.runtime_status or "").strip().lower()
    reason = str(facts.runtime_reason or "").strip()
    if status == "ok":
        return CloseoutOutcome("done", "", "done")
    if status == "cancelled":
        return CloseoutOutcome("cancelled", reason or "user_stop", "cancelled")
    if facts.sleeping:
        return CloseoutOutcome("sleep_wait", "clock_sleep", "sleep_wait")
    if not facts.continuable:
        return CloseoutOutcome("wait_human", reason or "not_continuable", "wait_human")
    if not facts.active_goal:
        return CloseoutOutcome("wait_handoff", "no_active_goal", "wait_handoff")
    if int(facts.same_reason_streak or 0) >= 3:
        return CloseoutOutcome(
            "wait_handoff", "same_reason_resume_limit_reached", "wait_handoff"
        )
    if int(facts.resume_budget_left or 0) == 0:
        return CloseoutOutcome("wait_handoff", "resume_limit_reached", "wait_handoff")
    if int(facts.rounds or 0) >= int(facts.max_rounds or 0):
        return CloseoutOutcome("wait_handoff", "max_rounds_reached", "wait_handoff")
    return CloseoutOutcome("resume_round", reason or "continuable", "resume_round")


_STATUSES = ["ok", "cancelled", "unfinished", "blocked", "context_overflow"]
_REASONS = ["", "TOOL_ROUND_LIMIT_REACHED", "PROTOCOL_VIOLATION"]
_STREAKS = [0, 2, 3]
_BUDGETS = [0, 1, 2]
_ROUND_CAPS = [(1, 9), (9, 9), (10, 9)]


@pytest.mark.parametrize(
    "status,reason,continuable,goal,streak,budget,rounds,max_rounds,sleeping",
    list(
        product(
            _STATUSES, _REASONS, [False, True], [False, True],
            _STREAKS, _BUDGETS, *zip(*_ROUND_CAPS), [False, True],
        )
    ),
)
def test_truth_table_full_sweep(
    status, reason, continuable, goal, streak, budget, rounds, max_rounds, sleeping
):
    facts = CloseoutFacts(
        runtime_status=status,
        runtime_reason=reason,
        runtime_source="tool_loop",
        continuable=continuable,
        active_goal=goal,
        resume_budget_left=budget,
        same_reason_streak=streak,
        rounds=rounds,
        max_rounds=max_rounds,
        sleeping=sleeping,
    )
    assert decide_closeout(facts) == _oracle(facts)


def test_ok_closeout_is_done_regardless_of_goal_and_budget():
    """模型自然收口 ok → done, 与 goal/预算无关(EXEC-38 无产出门语义)。"""
    facts = CloseoutFacts(
        runtime_status="ok", runtime_reason="", runtime_source="tool_loop",
        continuable=False, active_goal=True, resume_budget_left=9,
        same_reason_streak=0, rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "done"


def test_cancelled_is_terminal():
    """用户停止 → cancelled 终态, 不写移交单。"""
    facts = CloseoutFacts(
        runtime_status="cancelled", runtime_reason="user_stop",
        runtime_source="conversation_control", continuable=False,
        active_goal=True, resume_budget_left=9, same_reason_streak=0,
        rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "cancelled"
    assert outcome.reason == "user_stop"


def test_blocked_protocol_violation_wait_human():
    """blocked/协议违规(不可续跑族) → wait_human, 等用户显式继续。"""
    facts = CloseoutFacts(
        runtime_status="blocked", runtime_reason="PROTOCOL_VIOLATION",
        runtime_source="tool_protocol_adapter", continuable=False,
        active_goal=True, resume_budget_left=9, same_reason_streak=0,
        rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "wait_human"
    assert outcome.reason == "PROTOCOL_VIOLATION"


def test_no_active_goal_means_no_auto_resume_exit39():
    """EXEC-39: 可续跑族但无 active goal → wait_handoff(正常不自动续跑),
    不写移交单(避免被活着的 gateway 调度器自动接管)。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop", continuable=True, active_goal=False,
        resume_budget_left=9, same_reason_streak=0, rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "wait_handoff"
    assert outcome.reason == "no_active_goal"


def test_active_goal_with_budget_resumes():
    """EXEC-39 授权 + 护栏全过 → resume_round(goal-round-driver 同款)。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop", continuable=True, active_goal=True,
        resume_budget_left=2, same_reason_streak=0, rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "resume_round"
    assert outcome.reason == "TOOL_ROUND_LIMIT_REACHED"


def test_same_reason_streak_three_forces_handoff_exit30():
    """EXEC-30: 连续同因 3 次 → 收敛移交, 即使 goal/预算都还有。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop", continuable=True, active_goal=True,
        resume_budget_left=9, same_reason_streak=3, rounds=2, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "wait_handoff"
    assert outcome.reason == "same_reason_resume_limit_reached"


def test_budget_exhausted_forces_handoff():
    """resume_limit 预算耗尽 → 移交(写 budget_exhausted 事件由调用方做)。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
        runtime_source="tool_loop", continuable=True, active_goal=True,
        resume_budget_left=0, same_reason_streak=0, rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "wait_handoff"
    assert outcome.reason == "resume_limit_reached"


def test_negative_budget_means_unbounded():
    """<0 的预算=无预算概念(手动续跑/无 policy 场景): 不因预算停。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop", continuable=True, active_goal=True,
        resume_budget_left=-1, same_reason_streak=0, rounds=1, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "resume_round"


def test_max_rounds_reached_forces_handoff():
    """进程内轮数护栏(总代数上限) → 移交 max_rounds_reached。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop", continuable=True, active_goal=True,
        resume_budget_left=9, same_reason_streak=0, rounds=9, max_rounds=9,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "wait_handoff"
    assert outcome.reason == "max_rounds_reached"


def test_guidance_key_matches_state_invariants():
    """guidance_key 与 state 一一对应, 文案选择不得跨态借用。"""
    mapping = {
        "done": "done",
        "cancelled": "cancelled",
        "wait_human": "wait_human",
        "wait_handoff": "wait_handoff",
        "resume_round": "resume_round",
        "sleep_wait": "sleep_wait",
    }
    combos = list(
        product(
            ["unfinished"], ["TOOL_ROUND_LIMIT_REACHED"], [False, True],
            [False, True], [0, 3], [0, 2], [(1, 9), (9, 9)],
        )
    )
    for status, reason, continuable, goal, streak, budget, cap in combos:
        facts = CloseoutFacts(
            runtime_status=status, runtime_reason=reason, runtime_source="tool_loop",
            continuable=continuable, active_goal=goal, resume_budget_left=budget,
            same_reason_streak=streak, rounds=cap[0], max_rounds=cap[1],
        )
        outcome = decide_closeout(facts)
        assert mapping[outcome.state] == outcome.guidance_key


def test_sleep_wait_closeout_keeps_task_non_terminal():
    """调度改造 2b: 模型睡完收口 → sleep_wait, 与 goal/预算/可续跑无关。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="CLOCK_SLEEP_WAITING",
        runtime_source="clock_sleep_tool", continuable=False, active_goal=True,
        resume_budget_left=9, same_reason_streak=0, rounds=1, max_rounds=9,
        sleeping=True,
    )
    outcome = decide_closeout(facts)
    assert outcome.state == "sleep_wait"
    assert outcome.reason == "clock_sleep"
    assert outcome.guidance_key == "sleep_wait"


def test_sleep_never_becomes_auto_resume_round():
    """sleeping 即使 active_goal+预算充足也不进 resume_round(睡觉不作废)。"""
    facts = CloseoutFacts(
        runtime_status="unfinished", runtime_reason="CLOCK_SLEEP_WAITING",
        runtime_source="clock_sleep_tool", continuable=True, active_goal=True,
        resume_budget_left=9, same_reason_streak=0, rounds=1, max_rounds=9,
        sleeping=True,
    )
    assert decide_closeout(facts).state == "sleep_wait"


def test_user_cancel_beats_sleep():
    """用户中断(cancelled)优先级高于 sleep: 闹钟先死。"""
    facts = CloseoutFacts(
        runtime_status="cancelled", runtime_reason="user_stop",
        runtime_source="conversation_control", continuable=False,
        active_goal=False, resume_budget_left=0, same_reason_streak=0,
        rounds=1, max_rounds=9, sleeping=True,
    )
    assert decide_closeout(facts).state == "cancelled"


def test_ok_status_beats_sleeping_flag():
    """runtime ok 仍优先 done(工具循环只在非 ok 时才标 sleeping, 防误杀)。"""
    facts = CloseoutFacts(
        runtime_status="ok", runtime_reason="", runtime_source="tool_loop",
        continuable=False, active_goal=False, resume_budget_left=0,
        same_reason_streak=0, rounds=1, max_rounds=9, sleeping=True,
    )
    assert decide_closeout(facts).state == "done"
