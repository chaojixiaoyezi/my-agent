
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)


def validate_failure_sample_library(cases: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    if not cases:
        findings.append(finding("FAILURE_SAMPLE_LIBRARY_EMPTY"))
        return validation_report(findings)
    for case in cases:
        _validate_case(case, findings)
    return validation_report(findings)


def _validate_case(case: dict[str, Any], findings: list[dict[str, object]]) -> None:
    _require_ref(case, "contract_fixture_ref", "FAILURE_SAMPLE_CONTRACT_FIXTURE_MISSING", findings)
    _require_ref(case, "fake_tool_trace_ref", "FAILURE_SAMPLE_FAKE_TOOL_TRACE_MISSING", findings)
    _require_ref(case, "fake_llm_trace_ref", "FAILURE_SAMPLE_FAKE_LLM_TRACE_MISSING", findings)
    _require_ref(case, "replay_spec_ref", "FAILURE_SAMPLE_REPLAY_SPEC_MISSING", findings)
    if not string_tuple(case.get("expected_error_codes")):
        findings.append(finding("FAILURE_SAMPLE_EXPECTED_ERRORS_MISSING", _case_extra(case)))
    _require_ref(case, "regression_test_ref", "FAILURE_SAMPLE_REGRESSION_TEST_MISSING", findings)


def _require_ref(case: dict[str, Any], key: str, code: str, findings: list[dict[str, object]]) -> None:
    if not text(case.get(key)):
        findings.append(finding(code, _case_extra(case)))


def _case_extra(case: dict[str, Any]) -> dict[str, object]:
    return {"case_id": text(case.get("case_id")), "failure_type": text(case.get("failure_type"))}


__all__ = ["validate_failure_sample_library"]
