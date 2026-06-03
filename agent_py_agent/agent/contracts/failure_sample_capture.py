
from __future__ import annotations

from typing import Any

from ..common.value_parsing import sequence_strings


def failure_samples_from_case_results(results: tuple[object, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        sample
        for result in results
        for sample in (_sample_from_case(_case_dict(result)),)
        if sample
    )


def _sample_from_case(case: dict[str, Any]) -> dict[str, Any]:
    if str(case.get("status") or "").strip() not in {"FAILED", "BLOCKED"}:
        return {}
    case_id = str(case.get("case_id") or "unknown_case").strip() or "unknown_case"
    issues = sequence_strings(case.get("issues")) or ["UNKNOWN_FAILURE"]
    return {
        "case_id": case_id,
        "failure_type": _failure_type(issues),
        "contract_fixture_ref": _ref(case, "contract_fixture_ref", f"contract-fixture://{case_id}.yaml"),
        "fake_tool_trace_ref": _ref(case, "fake_tool_trace_ref", f"trace://{case_id}/tool.jsonl"),
        "fake_llm_trace_ref": _ref(case, "fake_llm_trace_ref", f"trace://{case_id}/llm.jsonl"),
        "replay_spec_ref": _ref(case, "replay_spec_ref", f"replay://{case_id}.json"),
        "expected_error_codes": issues,
        "regression_test_ref": _ref(case, "regression_test_ref", f"pytest://replay/{case_id}"),
        "source": "pre_real_task_validation",
    }


def _case_dict(result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {
        key: getattr(result, key)
        for key in ("case_id", "status", "issues")
        if hasattr(result, key)
    }


def _failure_type(issues: list[str]) -> str:
    first = issues[0].lower().strip()
    if "artifact" in first:
        return "artifact_contract"
    if "tool" in first:
        return "tool_contract"
    if "timeout" in first:
        return "timeout"
    return "contract"


def _ref(case: dict[str, Any], key: str, fallback: str) -> str:
    value = str(case.get(key) or "").strip()
    return value or fallback


__all__ = ["failure_samples_from_case_results"]
