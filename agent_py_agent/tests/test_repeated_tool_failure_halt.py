from __future__ import annotations

"""四级阶梯单测:软提示→收口自动续跑→收益递减→真硬门(默认关)。

真机实证(urllib3→Go 复刻,2026-08-06):send_message 工具 60+ 次连续失败
(TOOL_PARAMETER_TYPE_INVALID),模型每次调用都改参数 → (tool_name, args_hash)
身份从不累计,(tool, args_hash) guardrail 的 N/2N/3N 永不触发,死循环绕过去。

四级阶梯:
  L1 软提示    :同工具同类失败 2 次即按失败类别注入恢复指引,不拦调用。
  L2 收口续跑  :同类失败达阈值(默认 8)本轮收口,任务保持未完成并自动续跑,
                 清掉失败段,下轮换策略后从 0 重新累计。
  L3 收益递减  :连续 3 轮收口且每轮无任何成功工具结果 → 真收口,等用户。
  L4 真硬门    :默认关;开启后同类失败达 15 次强制真收口等用户。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._finalization_service import (
    _conversation_turn_is_terminal,
)
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _mark_repeated_failure_halt,
    _recovery_hint_for_failure_class,
    _repeated_failure_streak,
)
from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
    clear_consecutive_failure_segment,
)
from agent_py_agent.agent.contracts.gates.tool_guardrail import (
    consecutive_same_failure_count,
    failure_class_of_result,
)

_HALT_THRESHOLD = 8
_HARD_THRESHOLD = 15


def _failed_result(error_code: str = "TOOL_PARAMETER_TYPE_INVALID"):
    return SimpleNamespace(ok=False, error_code=error_code, error_category="", output=None)


def _ok_result():
    return SimpleNamespace(ok=True, error_code="", error_category="", output="fine")


def _records_from(results):
    """把一串 (tool_name, result) 折叠成 guardrail records 序列。"""
    return tuple(
        {
            "tool_name": tool_name,
            "args_hash": f"args-{i}",
            "failed": not result.ok,
            "failure_class": failure_class_of_result(result) if not result.ok else "",
        }
        for i, (tool_name, result) in enumerate(results)
    )


def _agent_with_records(records, streak=0):
    agent = SimpleNamespace(_tool_call_guardrail_records=records)
    if streak:
        agent._repeated_failure_halt_streak = streak
    return agent


def _record(
    tool_name,
    result,
    repeated_failure_halt=None,
    tool_context=None,
    executed_tools=None,
    hard_enabled=False,
):
    params = SimpleNamespace(
        repeated_failure_halt=repeated_failure_halt,
        repeated_failure_halt_exhausted=False,
        tool_context=tool_context if tool_context is not None else [],
        task_attributes={
            "repeated_failure_halt_threshold": _HALT_THRESHOLD,
            "hard_failure_halt_enabled": hard_enabled,
            "hard_failure_halt_threshold": _HARD_THRESHOLD,
        },
        runtime_guard_policy=None,
        executed_tools=executed_tools if executed_tools is not None else [],
    )
    call = SimpleNamespace(tool_name=tool_name)
    return SimpleNamespace(params=params, call=call, result=result)


class TestFailureClassOfResult:
    def test_error_code_wins_over_category(self):
        result = _failed_result(error_code="TOOL_PARAMETER_TYPE_INVALID")
        assert failure_class_of_result(result) == "code:TOOL_PARAMETER_TYPE_INVALID"

    def test_category_fallback(self):
        result = SimpleNamespace(
            ok=False, error_code="", error_category="parameter_error", output=None
        )
        assert failure_class_of_result(result) == "category:parameter_error"

    def test_output_hash_last_resort(self):
        result = SimpleNamespace(ok=False, error_code="", error_category="", output='{"x": 1}')
        assert failure_class_of_result(result).startswith("output:")


class TestConsecutiveSameFailureCount:
    def test_accumulates_same_class_even_with_different_args(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(3)]
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 3
        )

    def test_different_failure_class_resets_to_one(self):
        records = _records_from(
            [
                ("send_message", _failed_result("TOOL_PARAMETER_TYPE_INVALID")),
                ("send_message", _failed_result("TOOL_EXECUTION_TIMEOUT")),
            ]
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_EXECUTION_TIMEOUT"
            )
            == 1
        )

    def test_success_resets_to_zero(self):
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _ok_result()),
            ]
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 0
        )

    def test_different_class_midway_clears_segment(self):
        records = _records_from(
            [
                ("send_message", _failed_result("TOOL_PARAMETER_TYPE_INVALID")),
                ("send_message", _failed_result("TOOL_EXECUTION_TIMEOUT")),
                ("send_message", _failed_result("TOOL_EXECUTION_TIMEOUT")),
            ]
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_EXECUTION_TIMEOUT"
            )
            == 2
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 0
        )

    def test_other_tools_do_not_participate(self):
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("run_command", _failed_result("TOOL_EXECUTION_TIMEOUT")),
                ("send_message", _failed_result()),
            ]
        )
        assert (
            consecutive_same_failure_count(
                records, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 2
        )


class TestSoftHint:
    """L1:同工具同类失败 2 次注入按失败类别的恢复指引,不拦调用。"""

    def test_hint_injected_at_count_two(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(2)]
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        joined = "\n".join(record.params.tool_context)
        assert "连续 2 次失败" in joined
        assert "不要原样重试" in joined

    def test_no_hint_below_count_two(self):
        records = _records_from([("send_message", _failed_result())])
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.tool_context == []

    def test_hint_only_at_count_two(self):
        # 每次失败都是新 record:提示只在 count 恰为 2 的那次注入,count==3 不重复。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(3)]
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.tool_context == []


class TestRecoveryHintForFailureClass:
    def test_parameter_hint(self):
        hint = _recovery_hint_for_failure_class("code:TOOL_PARAMETER_TYPE_INVALID")
        assert "参数名称、类型与必填项" in hint

    def test_permission_hint(self):
        hint = _recovery_hint_for_failure_class("code:TOOL_PERMISSION_DENIED")
        assert "权限" in hint

    def test_timeout_hint(self):
        hint = _recovery_hint_for_failure_class("code:TOOL_EXECUTION_TIMEOUT")
        assert "超时" in hint

    def test_unknown_tool_hint(self):
        hint = _recovery_hint_for_failure_class("code:TOOL_NOT_FOUND")
        assert "不存在的工具" in hint

    def test_generic_fallback(self):
        hint = _recovery_hint_for_failure_class("code:SOMETHING_UNEXPECTED")
        assert "换一种做法" in hint


class TestMarkRepeatedFailureHalt:
    def test_halt_after_threshold(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt == (
            "send_message",
            "code:TOOL_PARAMETER_TYPE_INVALID",
            _HALT_THRESHOLD,
        )
        assert not record.params.repeated_failure_halt_exhausted, "首轮软收口"
        assert record.params.tool_context, "必须给模型留下收口提示"

    def test_no_halt_below_threshold(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD - 1)]
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt is None

    def test_guardrail_deny_does_not_pollute_segment(self):
        # guardrail 自身拦截记录(BLOCKED)不计入连续段:不累计也不清零。
        blocked_records = (
            {
                "tool_name": "send_message",
                "args_hash": "args-x",
                "failed": True,
                "failure_class": "code:TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
            },
        )
        assert (
            consecutive_same_failure_count(
                blocked_records,
                "send_message",
                "code:TOOL_PARAMETER_TYPE_INVALID",
            )
            == 0
        )
        mixed = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        ) + blocked_records + _records_from([("send_message", _failed_result())])
        assert (
            consecutive_same_failure_count(
                mixed,
                "send_message",
                "code:TOOL_PARAMETER_TYPE_INVALID",
            )
            == _HALT_THRESHOLD + 1
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(mixed), record)
        assert record.params.repeated_failure_halt is not None

    def test_deny_then_more_failures_halt(self):
        # DENY 之后模型继续同类失败 → 收口(真机 60+ 次循环的兜底路径)。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        ) + (
            {
                "tool_name": "send_message",
                "args_hash": "args-x",
                "failed": True,
                "failure_class": "code:TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
            },
        ) + _records_from([("send_message", _failed_result())])
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt == (
            "send_message",
            "code:TOOL_PARAMETER_TYPE_INVALID",
            _HALT_THRESHOLD + 1,
        )

    def test_no_halt_on_success(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        record = _record("send_message", _ok_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt is None

    def test_halt_is_idempotent(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        record = _record(
            "send_message",
            _failed_result(),
            repeated_failure_halt=(
                "send_message",
                "code:TOOL_PARAMETER_TYPE_INVALID",
                _HALT_THRESHOLD,
            ),
        )
        context_len = len(record.params.tool_context)
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt is not None
        assert len(record.params.tool_context) == context_len, "已收口后不得重复追加提示"

    def test_soft_halt_clears_failure_segment(self):
        # L2 软收口后清掉该失败段:下轮换策略重试同一工具从 0 累计,不会一碰再收口。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        agent = _agent_with_records(records)
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(agent, record)
        assert not record.params.repeated_failure_halt_exhausted
        assert (
            consecutive_same_failure_count(
                _records_after(agent), "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 0
        )
        # 清段后同类失败 1 次只是 count=1,远不到阈值。
        one_more = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(agent, one_more)
        assert one_more.params.repeated_failure_halt is None

    def test_soft_halt_nudge_context(self):
        # L2 收口文案是 nudge(继续推进),不是"等用户介入"。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        record = _record("send_message", _failed_result(), executed_tools=["read_file"])
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        joined = "\n".join(record.params.tool_context)
        assert "继续推进" in joined
        assert "不要总结或宣告完成" in joined

    def test_hard_halt_when_enabled(self):
        # L4:显式开启硬门后,同类失败达 15 次即真收口(不自动续跑)。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HARD_THRESHOLD)]
        )
        record = _record("send_message", _failed_result(), hard_enabled=True)
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt_exhausted
        joined = "\n".join(record.params.tool_context)
        assert "等待用户提供新思路" in joined

    def test_hard_halt_disabled_by_default(self):
        # L4 默认关:即使 20 次同类失败也只软收口(自动续跑)。
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(20)]
        )
        record = _record("send_message", _failed_result(), hard_enabled=False)
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert not record.params.repeated_failure_halt_exhausted


def _records_after(agent):
    return agent._tool_call_guardrail_records


class TestRepeatedFailureExhausted:
    """L3 收益递减:连续 3 轮收口且每轮无成功工具结果 → 真收口等用户。"""

    def _run_halt_round(self, agent, executed_tools):
        agent._tool_call_guardrail_records = _records_from(
            [("send_message", _failed_result()) for _ in range(_HALT_THRESHOLD)]
        )
        record = _record(
            "send_message",
            _failed_result(),
            executed_tools=executed_tools,
        )
        _mark_repeated_failure_halt(agent, record)
        return record

    def test_exhausted_after_three_rounds_without_progress(self):
        agent = _agent_with_records(())
        r1 = self._run_halt_round(agent, [])
        assert not r1.params.repeated_failure_halt_exhausted
        r2 = self._run_halt_round(agent, [])
        assert not r2.params.repeated_failure_halt_exhausted
        r3 = self._run_halt_round(agent, [])
        assert r3.params.repeated_failure_halt_exhausted
        joined = "\n".join(r3.params.tool_context)
        assert "无法自行脱困" in joined

    def test_successful_tool_call_resets_streak(self):
        # 轮内有成功工具结果即算有进展:streak 归 1,不叠加。
        agent = _agent_with_records(())
        self._run_halt_round(agent, [])
        self._run_halt_round(agent, [])
        r3 = self._run_halt_round(agent, ["write_file"])
        assert not r3.params.repeated_failure_halt_exhausted
        assert _repeated_failure_streak(agent) == 1

    def test_exhausted_marks_streak(self):
        agent = _agent_with_records(())
        self._run_halt_round(agent, [])
        self._run_halt_round(agent, [])
        self._run_halt_round(agent, [])
        assert _repeated_failure_streak(agent) == 3


class TestClearConsecutiveFailureSegment:
    def test_clears_only_trailing_segment(self):
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(3)]
        ) + _records_from(
            [
                ("read_file", _ok_result()),
                ("send_message", _failed_result("TOOL_EXECUTION_TIMEOUT")),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
        )
        agent = _agent_with_records(records)
        clear_consecutive_failure_segment(
            agent, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
        )
        remaining = _records_after(agent)
        # 尾部的 2 条同类失败被清;其他工具、不同失败类记录保留。
        assert len(remaining) == len(records) - 2
        assert (
            consecutive_same_failure_count(
                remaining, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
            )
            == 0
        )

    def test_guardrail_records_survive(self):
        blocked = (
            {
                "tool_name": "send_message",
                "args_hash": "args-x",
                "failed": True,
                "failure_class": "code:TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
            },
        )
        records = _records_from(
            [("send_message", _failed_result()) for _ in range(3)]
        ) + blocked
        agent = _agent_with_records(records)
        clear_consecutive_failure_segment(
            agent, "send_message", "code:TOOL_PARAMETER_TYPE_INVALID"
        )
        remaining = _records_after(agent)
        assert blocked[0] in remaining


class TestConversationTurnIsTerminal:
    def test_repeated_failure_reason_keeps_task_active(self):
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(
                runtime_status="ok", runtime_reason="REPEATED_TOOL_FAILURE"
            )
        )
        assert _conversation_turn_is_terminal(ctx) is False

    def test_exhausted_reason_keeps_task_active(self):
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(
                runtime_status="ok", runtime_reason="REPEATED_TOOL_FAILURE_EXHAUSTED"
            )
        )
        assert _conversation_turn_is_terminal(ctx) is False

    def test_plain_turn_still_terminal(self):
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(runtime_status="ok", runtime_reason="")
        )
        assert _conversation_turn_is_terminal(ctx) is True
