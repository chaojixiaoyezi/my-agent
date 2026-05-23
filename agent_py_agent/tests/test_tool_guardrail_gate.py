# LLM: Tool guardrail gate tests verify loop detection (exact failure, same-tool failure, no-progress).
# 模块用途: 用结构化 records 模拟重复失败和无进展场景，确保 gate 在正确阈值触发 warn/block。

from __future__ import annotations

import time

from agent_py_agent.agent.contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    args_hash_for_guardrail,
    evaluate_tool_guardrail_gate,
    record_tool_guardrail_result,
    result_hash_for_guardrail,
)


def _make_record(tool_name: str, args_hash: str, failed: bool = False, result_hash: str = "") -> dict:
    r: dict[str, object] = {"tool_name": tool_name, "args_hash": args_hash, "failed": failed}
    if result_hash:
        r["result_hash"] = result_hash
    r["ts"] = time.time()
    return r


class TestExactFailureDetection:
    def test_allows_first_call(self):
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            records=(),
        )
        assert decision.allowed is True

    def test_warns_after_exact_failures_reach_threshold(self):
        config = ToolGuardrailConfig(exact_failure_warn_after=2)
        records = tuple(
            _make_record("read_file", "abc123", failed=True) for _ in range(2)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            config=config,
            records=records,
        )
        assert decision.allowed is True
        assert any("EXACT_FAILURE_WARNING" in f.code for f in decision.findings)

    def test_blocks_after_exact_failures_exceed_block_threshold(self):
        config = ToolGuardrailConfig(exact_failure_block_after=5)
        records = tuple(
            _make_record("read_file", "abc123", failed=True) for _ in range(5)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc123"),
            config=config,
            records=records,
        )
        assert decision.allowed is False
        assert any("EXACT_FAILURE_BLOCKED" in f.code for f in decision.findings)

    def test_resets_exact_count_on_success(self):
        config = ToolGuardrailConfig(exact_failure_warn_after=2)
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


class TestSameToolFailureDetection:
    def test_warns_after_same_tool_failures(self):
        config = ToolGuardrailConfig(same_tool_failure_warn_after=3)
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
        assert any("SAME_TOOL_FAILURE_WARNING" in f.code for f in decision.findings)

    def test_blocks_after_same_tool_failures_exceed(self):
        config = ToolGuardrailConfig(same_tool_failure_block_after=5)
        records = tuple(
            _make_record("terminal", f"hash{i}", failed=True) for i in range(5)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("terminal", "hash_x"),
            config=config,
            records=records,
        )
        assert decision.allowed is False
        assert any("SAME_TOOL_FAILURE_BLOCKED" in f.code for f in decision.findings)


class TestNoProgressDetection:
    def test_skips_mutating_tools(self):
        config = ToolGuardrailConfig(no_progress_warn_after=1)
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
        config = ToolGuardrailConfig(no_progress_warn_after=2)
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

    def test_blocks_on_readonly_no_progress(self):
        config = ToolGuardrailConfig(no_progress_block_after=3)
        records = tuple(
            _make_record("read_file", "abc", failed=False, result_hash="same")
            for _ in range(3)
        )
        decision = evaluate_tool_guardrail_gate(
            ToolGuardrailFacts("read_file", "abc", result_hash="same", is_readonly=True),
            config=config,
            records=records,
        )
        assert decision.allowed is False
        assert any("NO_PROGRESS_BLOCKED" in f.code for f in decision.findings)


class TestRecordToolGuardrailResult:
    def test_appends_record(self):
        records: tuple[dict[str, object], ...] = ()
        facts = ToolGuardrailFacts("pwd", "hash1", failed=False, result_hash="res1", now=100.0)
        new_records = record_tool_guardrail_result(records, facts)
        assert len(new_records) == 1
        assert new_records[0]["tool_name"] == "pwd"
        assert new_records[0]["failed"] is False

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
