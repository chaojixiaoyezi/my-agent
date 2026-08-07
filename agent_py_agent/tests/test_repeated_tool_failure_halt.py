from __future__ import annotations

"""同类失败强制收口单测。

真机实证（urllib3→Go 复刻，2026-08-06）：send_message 工具 60+ 次连续失败
（TOOL_PARAMETER_TYPE_INVALID），模型每次调用都改参数 → (tool_name, args_hash)
身份从不累计，(tool, args_hash) guardrail 的 N/2N/3N 永不触发，死循环绕过去。

修复：按 (tool_name, failure_class) 累计「连续同类失败」，达 3 次即在本轮
工具循环内强制收口为 REPEATED_TOOL_FAILURE 未完成态；finalize 侧把该原因
排除出「可关闭任务」判定，任务保持活跃，等用户介入换策略。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._finalization_service import (
    _conversation_turn_is_terminal,
)
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _mark_repeated_failure_halt,
)
from agent_py_agent.agent.contracts.gates.tool_guardrail import (
    consecutive_same_failure_count,
    failure_class_of_result,
)


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


def _agent_with_records(records):
    return SimpleNamespace(_tool_call_guardrail_records=records)


def _record(tool_name, result, repeated_failure_halt=None, tool_context=None):
    # N=1 → guardrail 3N DENY 在第 3 次失败,halt 阈值 4(DENY 后 1 次)。
    params = SimpleNamespace(
        repeated_failure_halt=repeated_failure_halt,
        tool_context=tool_context if tool_context is not None else [],
        task_attributes={"repeat_fail_threshold": 1},
        runtime_guard_policy=None,
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
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
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


class TestMarkRepeatedFailureHalt:
    def test_halt_after_threshold(self):
        # N=1 → 阈值 4：guardrail 3N DENY 在第 3 次失败先行,其后同类再犯 1 次即收口。
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt == (
            "send_message",
            "code:TOOL_PARAMETER_TYPE_INVALID",
            4,
        )
        assert record.params.tool_context, "必须给模型留下强制收口提示"

    def test_no_halt_below_threshold(self):
        # N=1 → 阈值 4：3 次同类失败不够,留给 guardrail 的 DENY(3N)语义。
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
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
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
        ) + blocked_records + _records_from([("send_message", _failed_result())])
        assert (
            consecutive_same_failure_count(
                mixed,
                "send_message",
                "code:TOOL_PARAMETER_TYPE_INVALID",
            )
            == 4
        )
        record = _record("send_message", _failed_result())
        _mark_repeated_failure_halt(_agent_with_records(mixed), record)
        assert record.params.repeated_failure_halt is not None

    def test_deny_then_more_failures_halt(self):
        # DENY 之后模型继续同类失败 → 收口(真机 60+ 次循环的兜底路径)。
        # 模拟真实顺序:本次失败已由观察写入 records,再触发 halt 检查。
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
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
            4,
        )

    def test_no_halt_on_success(self):
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
        )
        record = _record("send_message", _ok_result())
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt is None

    def test_halt_is_idempotent(self):
        records = _records_from(
            [
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
                ("send_message", _failed_result()),
            ]
        )
        record = _record(
            "send_message",
            _failed_result(),
            repeated_failure_halt=("send_message", "code:TOOL_PARAMETER_TYPE_INVALID", 4),
        )
        context_len = len(record.params.tool_context)
        _mark_repeated_failure_halt(_agent_with_records(records), record)
        assert record.params.repeated_failure_halt is not None
        assert len(record.params.tool_context) == context_len, "已收口后不得重复追加提示"


class TestConversationTurnIsTerminal:
    def test_repeated_failure_reason_keeps_task_active(self):
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(
                runtime_status="ok", runtime_reason="REPEATED_TOOL_FAILURE"
            )
        )
        assert _conversation_turn_is_terminal(ctx) is False

    def test_plain_turn_still_terminal(self):
        ctx = SimpleNamespace(
            final_response=SimpleNamespace(runtime_status="ok", runtime_reason="")
        )
        assert _conversation_turn_is_terminal(ctx) is True
