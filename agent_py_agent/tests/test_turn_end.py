from __future__ import annotations

from agent_py_agent.agent.turn_end import (
    infer_turn_end_reason,
    normalize_turn_end_reason,
    subagent_outcome_for_turn_end,
)


def test_turn_end_reason_uses_host_facts_not_prose():
    assert infer_turn_end_reason(runtime_status="ok") == "completed"
    assert infer_turn_end_reason(runtime_status="blocked") == "blocked"
    assert infer_turn_end_reason(runtime_status="cancelled") == "aborted"
    assert infer_turn_end_reason(runtime_status="error") == "error"
    assert (
        infer_turn_end_reason(
            runtime_status="unfinished",
            runtime_reason="MODEL_RESPONSE_TRUNCATED",
        )
        == "max-tokens"
    )


def test_turn_end_reason_rejects_unknown_explicit_value():
    assert normalize_turn_end_reason("模型说完成") == ""
    assert infer_turn_end_reason(explicit="模型说完成", runtime_status="ok") == "completed"


def test_subagent_outcome_keeps_nonterminal_interruptions_resumable():
    assert subagent_outcome_for_turn_end("completed") == ("DONE", "", True)
    assert subagent_outcome_for_turn_end("blocked") == (
        "BLOCKED",
        "status_blocked",
        False,
    )
    assert subagent_outcome_for_turn_end("max-tokens") == (
        "PENDING",
        "model_error",
        False,
    )
    assert subagent_outcome_for_turn_end("interrupted") == ("PENDING", "", False)
