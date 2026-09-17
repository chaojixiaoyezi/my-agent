
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    args_hash_for_guardrail,
    evaluate_tool_guardrail_gate,
    record_tool_guardrail_result,
    result_hash_for_guardrail,
)


def _make_record(
    tool_name: str,
    args_hash: str,
    **kwargs,
) -> dict:
    failed = bool(kwargs.get("failed", False))
    result_hash = str(kwargs.get("result_hash") or "")
    failure_class = str(kwargs.get("failure_class") or "code:failed")
    r: dict[str, object] = {"tool_name": tool_name, "args_hash": args_hash, "failed": failed}
    if result_hash:
        r["result_hash"] = result_hash
    if failed and failure_class:
        r["failure_class"] = failure_class
    r["ts"] = time.time()
    return r


class TestRepeatFailureDetection:
    def test_allows_first_call(self):
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            records=(),
        )
        assert decision.allowed is True

    def test_warns_after_repeat_failures_reach_threshold(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = tuple(
            _make_record("read_file", "abc123", failed=True, failure_class="code:not_found")
            for _ in range(2)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert any("REPEAT_FAILURE_HINT" in f.code for f in decision.findings)

    def test_blocks_only_the_next_same_call_at_three_times_threshold(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = tuple(
            _make_record("read_file", "abc123", failed=True, failure_class="code:not_found")
            for _ in range(6)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            config=config,
            records=records,
        )
        assert decision.allowed is False
        assert decision.recommended_action == "change_strategy"
        assert any("REPEAT_FAILURE_BLOCKED" in f.code for f in decision.findings)

    def test_zero_threshold_is_unlimited_with_fixed_hints(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=0)
        records = tuple(
            _make_record("read_file", "abc123", failed=True, result_hash="same")
            for _ in range(50)
        )

        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123", result_hash="same"),
            config=config,
            records=records,
        )

        assert decision.allowed is True
        assert any("REPEAT_FAILURE_HINT" in f.code for f in decision.findings)

    def test_resets_exact_count_on_success(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = (
            _make_record("read_file", "abc123", failed=True),
            _make_record("read_file", "abc123", failed=True),
            _make_record("read_file", "abc123", failed=False),
            _make_record("read_file", "abc123", failed=True),
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert not decision.findings

    def test_different_args_do_not_compound(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = (
            _make_record("terminal", "hash1", failed=True),
            _make_record("terminal", "hash2", failed=True),
            _make_record("terminal", "hash3", failed=True),
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("terminal", "hash4"),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert not decision.findings

    def test_different_failure_class_does_not_compound(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = (
            _make_record("terminal", "h1", failed=True, failure_class="code:not_found"),
            _make_record("terminal", "h1", failed=True, failure_class="code:not_found"),
            _make_record("terminal", "h1", failed=True, failure_class="code:timeout"),
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("terminal", "h1"),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert not decision.findings


class TestNoProgressDetection:
    def test_skips_mutating_tools(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=1)
        records = tuple(
            _make_record("write_file", "abc", failed=False, result_hash="same")
            for _ in range(3)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("write_file", "abc", is_readonly=False),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert not decision.findings

    def test_warns_on_readonly_no_progress(self):
        config = ToolGuardrailConfig(readonly_no_progress_threshold=2)
        records = tuple(
            _make_record("read_file", "abc", failed=False, result_hash="same")
            for _ in range(2)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc", result_hash="same", is_readonly=True),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert any("NO_PROGRESS_WARNING" in f.code for f in decision.findings)

    def test_blocks_next_readonly_call_at_three_times_threshold(self):
        config = ToolGuardrailConfig(readonly_no_progress_threshold=2)
        records = tuple(
            _make_record("read_file", "abc", failed=False, result_hash="same")
            for _ in range(6)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc", result_hash="same", is_readonly=True),
            config=config,
            records=records,
        )
        assert decision.allowed is False
        assert any("NO_PROGRESS_BLOCKED" in f.code for f in decision.findings)

    def test_changing_readonly_results_count_as_progress(self):
        config = ToolGuardrailConfig(repeat_fail_threshold=2)
        records = (
            _make_record("read_file", "abc", failed=False, result_hash="page-1"),
            _make_record("read_file", "abc", failed=False, result_hash="page-2"),
            _make_record("read_file", "abc", failed=False, result_hash="page-3"),
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc", result_hash="page-3", is_readonly=True),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert not decision.findings


class TestRecordToolGuardrailResult:
    @pytest.mark.parametrize("kind", ["NO_PROGRESS", "REPEAT_FAILURE"])
    def test_own_rejections_do_not_reset_or_evict_observations(self, kind):
        failed = kind == "REPEAT_FAILURE"
        facts = ToolGuardrailFacts(
            "run_command", "same", failed=failed, is_readonly=True,
            failure_class="code:TOOL_TIMEOUT" if failed else "",
            result_hash="" if failed else "same-result", handler_executed=True,
        )
        records = ()
        for _ in range(9):
            records = record_tool_guardrail_result(records, facts)
        cfg = ToolGuardrailConfig(repeat_fail_threshold=3, readonly_no_progress_threshold=3)
        rejection = ToolGuardrailFacts(
            "run_command", "same", failed=True,
            failure_class=f"code:TOOL_GUARDRAIL_{kind}_BLOCKED", handler_executed=False,
        )
        for _ in range(300):
            decision = evaluate_tool_guardrail_gate(facts, config=cfg, records=records)
            assert not decision.allowed
            assert decision.evidence["count"] == 9
            assert not decision.block_task
            records = record_tool_guardrail_result(records, rejection)
        assert len(records) == 9

    @pytest.mark.parametrize("handler_executed", [False, True])
    def test_only_unexecuted_guard_rejection_is_excluded(self, handler_executed):
        facts = ToolGuardrailFacts(
            "read_file", "same", failed=True,
            failure_class="code:TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
            handler_executed=handler_executed,
        )
        assert len(record_tool_guardrail_result((), facts)) == int(handler_executed)

    def test_other_denial_and_error_text_are_not_guard_rejections(self):
        for error in ["code:WRITE_FORBIDDEN", "output:TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"]:
            assert len(record_tool_guardrail_result((), ToolGuardrailFacts(
                "read_file", "same", failed=True, failure_class=error,
            ))) == 1

    def test_existing_rejection_record_does_not_clear_same_result(self):
        records = tuple(_make_record("run_command", "same", result_hash="result") for _ in range(9))
        records += (_make_record("run_command", "same", failed=True,
                                 failure_class="code:TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"),)
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("run_command", "same", result_hash="result"), records=records,
        )
        assert not decision.allowed
        assert decision.evidence["count"] == 9

    def test_appends_record(self):
        records: tuple[dict[str, object], ...] = ()
        facts = ToolGuardrailFacts("pwd", "hash1", failed=True, result_hash="res1", failure_class="code:x", now=100.0)
        new_records = record_tool_guardrail_result(records, facts)
        assert len(new_records) == 1
        assert new_records[0]["tool_name"] == "pwd"
        assert new_records[0]["failed"] is True
        assert new_records[0]["failure_class"] == "code:x"

    def test_enforces_max_records(self):
        records = tuple({"tool_name": "pwd", "args_hash": f"h{i}", "failed": False} for i in range(300))
        facts = ToolGuardrailFacts("pwd", "new_hash")
        new_records = record_tool_guardrail_result(records, facts, max_records=256)
        assert len(new_records) == 256


class TestArgsHashForGuardrail:
    def test_same_args_same_hash(self):
        h1 = args_hash_for_guardrail("pwd", {"cwd": "/tmp"})
        h2 = args_hash_for_guardrail("pwd", {"cwd": "/tmp"})
        assert h1 == h2

    def test_different_args_different_hash(self):
        h1 = args_hash_for_guardrail("pwd", {"cwd": "/tmp"})
        h2 = args_hash_for_guardrail("pwd", {"cwd": "/var"})
        assert h1 != h2

    def test_different_tool_different_hash(self):
        h1 = args_hash_for_guardrail("pwd", {})
        h2 = args_hash_for_guardrail("ls", {})
        assert h1 != h2


class TestResultHashForGuardrail:
    def test_same_result_same_hash(self):
        h1 = result_hash_for_guardrail('{"ok": true}')
        h2 = result_hash_for_guardrail('{"ok": true}')
        assert h1 == h2

    def test_different_result_different_hash(self):
        h1 = result_hash_for_guardrail('{"ok": true}')
        h2 = result_hash_for_guardrail('{"ok": false}')
        assert h1 != h2

    def test_empty_result(self):
        assert result_hash_for_guardrail("") == ""
        assert result_hash_for_guardrail(None) == ""
