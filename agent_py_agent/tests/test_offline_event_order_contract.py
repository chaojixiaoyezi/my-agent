from __future__ import annotations


def test_event_order_ignores_duplicates_and_rejects_out_of_order_tool_result() -> None:
    from agent_py_agent.agent.contracts.offline_event_order_contract import validate_event_order

    result = validate_event_order(
        run_id="run-1",
        events=(
            {"event_id": "e1", "run_id": "run-1", "type": "TASK_STARTED"},
            {"event_id": "e1", "run_id": "run-1", "type": "TASK_STARTED"},
            {"event_id": "e2", "run_id": "run-1", "type": "TOOL_OK"},
        ),
    )

    assert result.ok is False
    assert result.error_codes == ("EVENT_OUT_OF_ORDER",)
    assert result.ignored_event_ids == ("e1",)


def test_event_order_rejects_late_tool_and_late_approval() -> None:
    from agent_py_agent.agent.contracts.offline_event_order_contract import validate_event_order

    result = validate_event_order(
        run_id="run-1",
        events=(
            {"event_id": "e1", "run_id": "run-1", "type": "TASK_CANCELLED"},
            {"event_id": "e2", "run_id": "run-1", "type": "TOOL_OK"},
            {"event_id": "e3", "run_id": "run-1", "type": "APPROVED"},
        ),
    )

    assert result.ok is False
    assert result.error_codes == ("EVENT_AFTER_TERMINAL", "APPROVAL_AFTER_BLOCKED")


def test_event_order_rejects_wrong_run_id() -> None:
    from agent_py_agent.agent.contracts.offline_event_order_contract import validate_event_order

    result = validate_event_order(run_id="run-2", events=({"event_id": "e1", "run_id": "run-1", "type": "TOOL_OK"},))

    assert result.ok is False
    assert result.error_codes == ("EVENT_RUN_ID_MISMATCH",)
