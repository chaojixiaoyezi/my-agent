from __future__ import annotations


def test_live_llm_fake_tool_trial_accepts_verified_fake_tool_run() -> None:
    from agent_py_agent.agent.contracts.live_llm_fake_tool_contract import (
        validate_live_llm_fake_tool_trial,
    )

    result = validate_live_llm_fake_tool_trial(
        {
            "real_llm": True,
            "tool_mode": "fake",
            "refs": {
                "prompt_ref": "artifact://trial/prompt.md",
                "response_ref": "artifact://trial/response.json",
                "tool_trace_ref": "artifact://trial/tool_trace.jsonl",
                "contract_hash": "abc123",
            },
            "final_claim": {"claimed_success": True},
            "verifier": {"ok": True},
            "metrics": {
                "unknown_tool_count": 0,
                "schema_error_count": 0,
                "tool_failure_claimed_success": False,
                "dry_run_claimed_real": False,
            },
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_live_llm_fake_tool_trial_rejects_fake_completion_and_tool_boundary_violations() -> None:
    from agent_py_agent.agent.contracts.live_llm_fake_tool_contract import (
        validate_live_llm_fake_tool_trial,
    )

    result = validate_live_llm_fake_tool_trial(
        {
            "real_llm": True,
            "tool_mode": "fake",
            "refs": {"prompt_ref": "artifact://trial/prompt.md"},
            "final_claim": {"claimed_success": True, "status": "succeeded"},
            "verifier": {"ok": False},
            "metrics": {
                "unknown_tool_count": 1,
                "schema_error_count": 1,
                "tool_failure_claimed_success": True,
                "dry_run_claimed_real": True,
            },
        }
    )

    assert result.error_codes == (
        "LIVE_LLM_TRIAL_REF_MISSING",
        "LIVE_LLM_FAKE_COMPLETION_REJECTED",
        "LIVE_LLM_UNKNOWN_TOOL",
        "LIVE_LLM_TOOL_SCHEMA_ERROR",
        "LIVE_LLM_TOOL_FAILURE_CLAIMED_SUCCESS",
        "LIVE_LLM_DRY_RUN_CLAIMED_REAL",
    )


def test_live_llm_fake_tool_trial_does_not_treat_status_text_as_success_claim() -> None:
    from agent_py_agent.agent.contracts.live_llm_fake_tool_contract import (
        validate_live_llm_fake_tool_trial,
    )

    result = validate_live_llm_fake_tool_trial(
        {
            "real_llm": True,
            "tool_mode": "fake",
            "refs": {
                "prompt_ref": "artifact://trial/prompt.md",
                "response_ref": "artifact://trial/response.json",
                "tool_trace_ref": "artifact://trial/tool_trace.jsonl",
                "contract_hash": "abc123",
            },
            "final_claim": {"status": "succeeded"},
            "verifier": {"ok": False},
            "metrics": {
                "unknown_tool_count": 0,
                "schema_error_count": 0,
                "tool_failure_claimed_success": False,
                "dry_run_claimed_real": False,
            },
        }
    )

    assert "LIVE_LLM_FAKE_COMPLETION_REJECTED" not in result.error_codes
