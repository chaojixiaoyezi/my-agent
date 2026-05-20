from __future__ import annotations


# LLM: Event order should be idempotent for duplicate events but reject impossible ordering.
# 函数用途: 验证重复事件被忽略，WAITING_TOOL 之前的 TOOL_OK 会被结构化事件顺序合同拒绝。
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


# LLM: Late events after terminal states should not revive a task.
# 函数用途: 验证 CANCELLED/BLOCKED 后迟到的工具或审批结果不会推进状态。
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


# LLM: Event run ids must match the active run scope.
# 函数用途: 验证旧 run 的事件不会误投递到当前 run。
def test_event_order_rejects_wrong_run_id() -> None:
    from agent_py_agent.agent.contracts.offline_event_order_contract import validate_event_order

    result = validate_event_order(run_id="run-2", events=({"event_id": "e1", "run_id": "run-1", "type": "TOOL_OK"},))

    assert result.ok is False
    assert result.error_codes == ("EVENT_RUN_ID_MISMATCH",)
