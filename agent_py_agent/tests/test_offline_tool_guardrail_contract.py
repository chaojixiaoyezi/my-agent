from __future__ import annotations

from pathlib import Path


def test_same_tool_same_args_blocks_on_third_unchanged_result() -> None:
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )

    event = {"type": "tool_result", "tool": "query_logs", "args_hash": "a", "result_hash": "r"}
    result = validate_tool_guardrail_events(
        (
            {**event, "operation_id": "op-1", "result": {"ok": True}},
            {**event, "operation_id": "op-2", "result": {"ok": True}},
            {**event, "operation_id": "op-3", "result": {"ok": True}},
        )
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_REPEATED_EXACT_RESULT",)


def test_same_tool_different_args_does_not_block() -> None:
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )

    result = validate_tool_guardrail_events(
        (
            {"type": "tool_result", "tool": "query_logs", "args_hash": "a1", "result_hash": "r", "result": {"ok": True}},
            {"type": "tool_result", "tool": "query_logs", "args_hash": "a2", "result_hash": "r", "result": {"ok": True}},
            {"type": "tool_result", "tool": "query_logs", "args_hash": "a3", "result_hash": "r", "result": {"ok": True}},
        )
    )

    assert result.ok is True


def test_failed_tool_retry_budget_stops_after_limit() -> None:
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )

    failure = {"type": "tool_result", "tool": "web_fetch", "args_hash": "same", "retryable": True}
    result = validate_tool_guardrail_events(
        (
            {**failure, "operation_id": "op-1", "result": {"ok": False}},
            {**failure, "operation_id": "op-2", "result": {"ok": False}},
            {**failure, "operation_id": "op-3", "result": {"ok": False}},
        ),
        retry_limit=2,
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_RETRY_LIMIT_EXCEEDED",)


def test_failed_tool_retry_budget_zero_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )

    failure = {
        "type": "tool_result",
        "tool": "web_fetch",
        "args_hash": "same",
        "retryable": True,
        "retry_limit": 0,
    }
    result = validate_tool_guardrail_events(
        (
            {**failure, "operation_id": "op-1", "result": {"ok": False}},
            {**failure, "operation_id": "op-2", "result": {"ok": False}},
            {**failure, "operation_id": "op-3", "result": {"ok": False}},
            {**failure, "operation_id": "op-4", "result": {"ok": False}},
        ),
        retry_limit=0,
    )

    assert result.ok is True


def test_repeated_threshold_zero_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )

    event = {"type": "tool_result", "tool": "query_logs", "args_hash": "a", "result_hash": "r"}
    result = validate_tool_guardrail_events(
        tuple({**event, "operation_id": f"op-{idx}", "result": {"ok": True}} for idx in range(10)),
        repeated_threshold=0,
    )

    assert result.ok is True


def test_fake_message_firewall_browser_and_ticket_tools_return_structured_results(tmp_path: Path) -> None:
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(tmp_path)
    message = runner.execute("send_message", {"target": "session:1", "idempotency_key": "msg-1"})
    firewall = runner.execute("block_ip", {"ip": "203.0.113.7", "mode": "dry_run"})
    browser = runner.execute("browser_open", {"url": "https://example.test"})
    ticket = runner.execute("create_ticket", {"title": "Investigate failed artifact"})

    assert message["ok"] is True
    assert firewall["mode"] == "dry_run"
    assert browser["dom_ref"] == "artifact://browser/dom-snapshot.json"
    assert ticket["ticket_id"]
