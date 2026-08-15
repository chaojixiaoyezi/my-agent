from __future__ import annotations


def test_side_effect_requires_tool_effect_declaration() -> None:
    from agent_py_agent.agent.contracts.offline_side_effect_contract import validate_side_effects

    result = validate_side_effects(({"type": "tool_registration", "tool": "send_message"},))

    assert result.ok is False
    assert result.error_codes == ("TOOL_EFFECT_MISSING",)


def test_side_effect_rejects_read_only_writes_and_missing_idempotency() -> None:
    from agent_py_agent.agent.contracts.offline_side_effect_contract import validate_side_effects

    result = validate_side_effects(
        (
            {"type": "tool_call", "tool": "query_logs", "effect": "read_only", "wrote_paths": ["report.md"]},
            {"type": "tool_call", "tool": "send_message", "effect": "mutating", "idempotency_key": ""},
        )
    )

    assert result.error_codes == ("READ_ONLY_TOOL_SIDE_EFFECT", "SIDE_EFFECT_IDEMPOTENCY_MISSING")


def test_side_effect_rejects_replay_execution_and_dry_run_claims() -> None:
    from agent_py_agent.agent.contracts.offline_side_effect_contract import validate_side_effects

    result = validate_side_effects(
        (
            {"type": "tool_call", "tool": "create_ticket", "effect": "mutating", "replay_mode": True, "executed": True},
            {"type": "tool_result", "tool": "block_ip", "mode": "dry_run", "claimed_real_execution": True},
        )
    )

    assert result.error_codes == ("SIDE_EFFECT_REPLAY_BLOCKED", "DRY_RUN_CLAIMED_REAL")
