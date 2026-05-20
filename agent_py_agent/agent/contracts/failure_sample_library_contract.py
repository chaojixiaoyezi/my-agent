# LLM: Failure sample library contracts keep real failures replayable before new real-task runs.
# 模块用途: 校验失败样本是否具备合同 fixture、fake tool、fake LLM、replay 和回归测试引用。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)


# LLM: validate_failure_sample_library is the replayability gate for known failures.
# 函数用途: 校验失败样本列表是否具备 fixture、fake trace、replay、expected errors 和回归测试。
def validate_failure_sample_library(cases: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    if not cases:
        findings.append(finding("FAILURE_SAMPLE_LIBRARY_EMPTY"))
        return validation_report(findings)
    for case in cases:
        _validate_case(case, findings)
    return validation_report(findings)


# LLM: _validate_case checks one failure sample for reproducibility refs.
# 函数用途: 对单个失败样本校验离线复现所需的机器引用。
def _validate_case(case: dict[str, Any], findings: list[dict[str, object]]) -> None:
    _require_ref(case, "contract_fixture_ref", "FAILURE_SAMPLE_CONTRACT_FIXTURE_MISSING", findings)
    _require_ref(case, "fake_tool_trace_ref", "FAILURE_SAMPLE_FAKE_TOOL_TRACE_MISSING", findings)
    _require_ref(case, "fake_llm_trace_ref", "FAILURE_SAMPLE_FAKE_LLM_TRACE_MISSING", findings)
    _require_ref(case, "replay_spec_ref", "FAILURE_SAMPLE_REPLAY_SPEC_MISSING", findings)
    if not string_tuple(case.get("expected_error_codes")):
        findings.append(finding("FAILURE_SAMPLE_EXPECTED_ERRORS_MISSING", _case_extra(case)))
    _require_ref(case, "regression_test_ref", "FAILURE_SAMPLE_REGRESSION_TEST_MISSING", findings)


# LLM: _require_ref verifies a required structured reference.
# 函数用途: 检查失败样本的某个 ref 字段是否存在，缺失时写稳定错误码。
def _require_ref(case: dict[str, Any], key: str, code: str, findings: list[dict[str, object]]) -> None:
    if not text(case.get(key)):
        findings.append(finding(code, _case_extra(case)))


# LLM: _case_extra keeps failure sample findings traceable.
# 函数用途: 给失败样本 finding 附加 case_id 和 failure_type。
def _case_extra(case: dict[str, Any]) -> dict[str, object]:
    return {"case_id": text(case.get("case_id")), "failure_type": text(case.get("failure_type"))}


__all__ = ["validate_failure_sample_library"]
