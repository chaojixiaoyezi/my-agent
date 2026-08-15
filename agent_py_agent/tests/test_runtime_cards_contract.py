from __future__ import annotations


def test_runtime_cards_allow_detached_long_task_with_checkpoint_and_route() -> None:
    from agent_py_agent.agent.contracts.runtime_cards import RuntimeCard, validate_runtime_card_set

    result = validate_runtime_card_set(
        [
            RuntimeCard(
                kind="session",
                card_id="session-1",
                owner_user_id="user-1",
                status="DETACHED",
                refs={"memory_home": "users/user-1"},
            ),
            RuntimeCard(
                kind="task",
                card_id="task-1",
                owner_user_id="user-1",
                session_id="session-1",
                status="RUNNING",
                refs={"checkpoint_ref": "tasks/task-1/checkpoint.json"},
            ),
            RuntimeCard(
                kind="notification_route",
                card_id="route-1",
                owner_user_id="user-1",
                session_id="session-1",
                task_id="task-1",
                status="ACTIVE",
                refs={"channel": "chat", "target": "session-1"},
            ),
        ]
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_runtime_cards_reject_long_task_without_checkpoint_or_route() -> None:
    from agent_py_agent.agent.contracts.runtime_cards import RuntimeCard, validate_runtime_card_set

    result = validate_runtime_card_set(
        [
            RuntimeCard(
                kind="task",
                card_id="task-1",
                owner_user_id="user-1",
                session_id="session-1",
                status="RUNNING",
                refs={},
            )
        ]
    )

    assert result.ok is False
    assert "TASK_CHECKPOINT_REF_MISSING" in result.error_codes
    assert "TASK_NOTIFICATION_ROUTE_MISSING" in result.error_codes


def test_runtime_cards_reject_worker_and_message_without_owner_links() -> None:
    from agent_py_agent.agent.contracts.runtime_cards import RuntimeCard, validate_runtime_card_set

    result = validate_runtime_card_set(
        [
            RuntimeCard(kind="worker", card_id="worker-1", owner_user_id="user-1", status="RUNNING"),
            RuntimeCard(
                kind="message",
                card_id="message-1",
                owner_user_id="user-1",
                task_id="task-1",
                status="PENDING",
                refs={"direction": "to_user"},
            ),
        ]
    )

    assert result.ok is False
    assert "WORKER_TASK_ID_MISSING" in result.error_codes
    assert "MESSAGE_ROUTE_MISSING" in result.error_codes
