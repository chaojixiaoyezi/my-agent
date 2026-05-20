from __future__ import annotations


def test_small_real_acceptance_gate_accepts_bounded_readonly_and_dry_run_cases() -> None:
    from agent_py_agent.agent.contracts.small_real_acceptance_gate import (
        validate_small_real_acceptance_gate,
    )

    result = validate_small_real_acceptance_gate({"cases": [_case("real-readonly"), _case("real-dry-run")]})

    assert result.ok is True
    assert result.error_codes == ()


def test_small_real_acceptance_gate_rejects_unbounded_or_mutating_cases() -> None:
    from agent_py_agent.agent.contracts.small_real_acceptance_gate import (
        validate_small_real_acceptance_gate,
    )

    case = _case("bad-real-case")
    case["complexity"] = "large"
    case["isolation_ok"] = False
    case["real_execution_allowed"] = True
    case["allowed_effects"] = ["read_only", "dangerous"]
    case["tool_modes"] = ["real_run"]
    case["expected_artifacts"] = []
    case["replay_capture_enabled"] = False

    result = validate_small_real_acceptance_gate({"cases": [case]})

    assert result.error_codes == (
        "SMALL_REAL_CASE_NOT_BOUNDED",
        "SMALL_REAL_ISOLATION_MISSING",
        "SMALL_REAL_REAL_EXECUTION_ENABLED",
        "SMALL_REAL_EFFECT_NOT_ALLOWED",
        "SMALL_REAL_TOOL_MODE_NOT_ALLOWED",
        "SMALL_REAL_ARTIFACT_ACCEPTANCE_MISSING",
        "SMALL_REAL_REPLAY_CAPTURE_MISSING",
    )


def _case(case_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "complexity": "small",
        "prompt_ref": f"prompt://{case_id}",
        "model_profile_ref": "model-profile://minimax-test",
        "workspace_ref": f"workspace://isolated/{case_id}",
        "isolation_ok": True,
        "tool_modes": ["read_only", "dry_run"],
        "allowed_effects": ["read_only", "dry_run"],
        "real_execution_allowed": False,
        "max_runtime_seconds": 300,
        "expected_artifacts": [{"artifact_contract_ref": f"contract://{case_id}/artifact"}],
        "verification_refs": [f"pytest://{case_id}/acceptance"],
        "replay_capture_enabled": True,
        "stop_on_failure": True,
    }
