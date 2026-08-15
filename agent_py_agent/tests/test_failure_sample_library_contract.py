from __future__ import annotations


def test_failure_sample_library_accepts_replayable_failure_cases() -> None:
    from agent_py_agent.agent.contracts.failure_sample_library_contract import (
        validate_failure_sample_library,
    )

    result = validate_failure_sample_library((_case("missing-artifact"), _case("tool-failed-fake-done")))

    assert result.ok is True
    assert result.error_codes == ()


def test_failure_sample_library_rejects_cases_without_replay_or_expected_errors() -> None:
    from agent_py_agent.agent.contracts.failure_sample_library_contract import (
        validate_failure_sample_library,
    )

    result = validate_failure_sample_library(
        (
            {
                "case_id": "bad-case",
                "failure_type": "missing_artifact",
                "contract_fixture_ref": "",
                "fake_tool_trace_ref": "",
                "fake_llm_trace_ref": "",
                "replay_spec_ref": "",
                "expected_error_codes": [],
                "regression_test_ref": "",
            },
        )
    )

    assert result.error_codes == (
        "FAILURE_SAMPLE_CONTRACT_FIXTURE_MISSING",
        "FAILURE_SAMPLE_FAKE_TOOL_TRACE_MISSING",
        "FAILURE_SAMPLE_FAKE_LLM_TRACE_MISSING",
        "FAILURE_SAMPLE_REPLAY_SPEC_MISSING",
        "FAILURE_SAMPLE_EXPECTED_ERRORS_MISSING",
        "FAILURE_SAMPLE_REGRESSION_TEST_MISSING",
    )


def _case(case_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "failure_type": "artifact_contract",
        "contract_fixture_ref": f"contract-fixture://{case_id}.yaml",
        "fake_tool_trace_ref": f"trace://{case_id}/tool.jsonl",
        "fake_llm_trace_ref": f"trace://{case_id}/llm.jsonl",
        "replay_spec_ref": f"replay://{case_id}.json",
        "expected_error_codes": ["ARTIFACT_MISSING"],
        "regression_test_ref": f"pytest://agent_py_agent/tests/replay/{case_id}",
        "source": "synthetic",
    }
