"""D-1: /stop with the user's snapshot cancels the exact turn a lease recovery requeued into the inbox."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _stop_requeued_exact_turn,
)
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

TURN = "gwreq-msg-requeued"


def _scope(turn_id: str = TURN, *, kind: str = "request", user: str = "u-1") -> GatewayControlScope:
    target = GatewayExecutionTarget("thread-1", kind, turn_id, "claim-old", "att-old")
    return GatewayControlScope(
        user_id=user, channel="tui", conversation_id="conv-1",
        metadata={"expected_turn_id": turn_id, "expected_execution": target.to_dict()},
    )


def _inbox_request(tmp_path: Path, *, recovery: bool = True) -> Path:
    paths = gateway_paths_from_root(tmp_path)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": TURN, "status": "pending", "priority": "recovery", "user_id": "u-1",
        "conversation": {"channel": "tui", "channel_conversation_id": "conv-1", "channel_user_id": "u-1"},
    }
    if recovery:
        payload["active_turn_recovery"] = {"request_id": TURN, "cause": "processing_lease_expired"}
    path = paths.inbox / f"{TURN}.json"
    write_json_file(path, payload)
    return path


@pytest.fixture
def interrupts(monkeypatch):
    names: list[str] = []
    monkeypatch.setattr(control_service, "interrupt_by_name", lambda name: names.append(name) or False)
    return names


def test_requeued_exact_turn_is_cancelled_and_old_claim_interrupted(tmp_path, interrupts):
    path = _inbox_request(tmp_path)
    result = _stop_requeued_exact_turn(gateway_paths_from_root(tmp_path), _scope())
    assert result is not None and result.ok and result.request_id == TURN
    payload = read_json_file(path)
    assert payload["cancel_requested"] is True and payload["control_status"] == "stopping"
    assert interrupts == [control_service.conversation_claim_interrupt_name("claim-old")]


@pytest.mark.parametrize("case", ["no_recovery_marker", "other_user", "other_turn", "task_target", "missing"])
def test_non_matching_requests_keep_the_original_rejection(tmp_path, interrupts, case):
    path = _inbox_request(tmp_path, recovery=case != "no_recovery_marker")
    scope = {
        "other_user": _scope(user="u-2"),
        "other_turn": _scope(turn_id="gwreq-msg-other"),
        "task_target": _scope(kind="task"),
    }.get(case, _scope())
    if case == "missing":
        path.unlink()
    assert _stop_requeued_exact_turn(gateway_paths_from_root(tmp_path), scope) is None
    if path.exists():
        assert "cancel_requested" not in read_json_file(path)


def test_already_stopping_request_is_not_reported_as_stopped(tmp_path, interrupts):
    path = _inbox_request(tmp_path)
    write_json_file(path, {**read_json_file(path), "cancel_requested": True})
    assert _stop_requeued_exact_turn(gateway_paths_from_root(tmp_path), _scope()) is None
