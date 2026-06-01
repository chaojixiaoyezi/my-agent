# LLM: Small real acceptance gates bound live validation before expensive complex real tasks.
# 模块用途: 校验小型真实验收 case 是否隔离、限时、只读或 dry-run、可验收、可回放。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..settings.defaults import default_agent_config
from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    positive_int,
    string_tuple,
    text,
    validation_report,
)

ALLOWED_COMPLEXITY = {"small", "medium"}
ALLOWED_EFFECTS = {"read_only", "dry_run"}
ALLOWED_TOOL_MODES = {"read_only", "dry_run"}


# LLM: _AllowedValuesCheck bundles one whitelist validation request.
# 类用途: 保存字段名、允许值集合和错误码，避免 helper 参数变宽。
@dataclass(frozen=True)
class _AllowedValuesCheck:
    key: str
    allowed: set[str]
    code: str


# LLM: validate_small_real_acceptance_gate is the pre-large-real-task gate.
# 函数用途: 校验小型真实验收 case 是否隔离、限时、只读或 dry-run、可验收且可 replay。
def validate_small_real_acceptance_gate(
    gate: dict[str, Any],
    *,
    config: object | None = None,
) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    cases = dict_items(gate.get("cases"))
    if not cases:
        findings.append(finding("SMALL_REAL_CASES_MISSING"))
        return validation_report(findings)
    max_runtime_seconds = _max_runtime_seconds(config)
    for case in cases:
        _validate_case(case, findings, max_runtime_seconds=max_runtime_seconds)
    return validation_report(findings)


# LLM: _validate_case checks one bounded live-validation case.
# 函数用途: 对单个小型真实验收 case 执行结构化边界校验。
def _validate_case(
    case: dict[str, Any],
    findings: list[dict[str, object]],
    *,
    max_runtime_seconds: int,
) -> None:
    if text(case.get("complexity")) not in ALLOWED_COMPLEXITY:
        findings.append(finding("SMALL_REAL_CASE_NOT_BOUNDED", _case_extra(case)))
    if case.get("isolation_ok") is not True or not text(case.get("workspace_ref")):
        findings.append(finding("SMALL_REAL_ISOLATION_MISSING", _case_extra(case)))
    if case.get("real_execution_allowed") is True:
        findings.append(finding("SMALL_REAL_REAL_EXECUTION_ENABLED", _case_extra(case)))
    _validate_allowed_values(case, _AllowedValuesCheck("allowed_effects", ALLOWED_EFFECTS, "SMALL_REAL_EFFECT_NOT_ALLOWED"), findings)
    _validate_allowed_values(case, _AllowedValuesCheck("tool_modes", ALLOWED_TOOL_MODES, "SMALL_REAL_TOOL_MODE_NOT_ALLOWED"), findings)
    if not dict_items(case.get("expected_artifacts")):
        findings.append(finding("SMALL_REAL_ARTIFACT_ACCEPTANCE_MISSING", _case_extra(case)))
    if not string_tuple(case.get("verification_refs")):
        findings.append(finding("SMALL_REAL_VERIFICATION_REF_MISSING", _case_extra(case)))
    if case.get("replay_capture_enabled") is not True:
        findings.append(finding("SMALL_REAL_REPLAY_CAPTURE_MISSING", _case_extra(case)))
    if max_runtime_seconds > 0 and positive_int(case.get("max_runtime_seconds")) > max_runtime_seconds:
        findings.append(finding("SMALL_REAL_RUNTIME_TOO_LARGE", _case_extra(case)))


# LLM: _max_runtime_seconds resolves the small-real runtime cap from AgentConfig.
# 函数用途: 读取小型真实验收声明运行时长上限；0 表示关闭这个上限 finding。
def _max_runtime_seconds(config: object | None) -> int:
    if config is None:
        config = default_agent_config()
    try:
        return max(0, int(config.small_real_acceptance_max_runtime_seconds))
    except (TypeError, ValueError):
        return 0


# LLM: _validate_allowed_values rejects effects or modes outside the bounded live gate.
# 函数用途: 校验字段值都在允许集合内，不从 prompt 文本推断工具风险。
def _validate_allowed_values(
    case: dict[str, Any],
    check: _AllowedValuesCheck,
    findings: list[dict[str, object]],
) -> None:
    values = string_tuple(case.get(check.key))
    if not values or any(value not in check.allowed for value in values):
        findings.append(finding(check.code, _case_extra(case)))


# LLM: _case_extra keeps findings traceable to a small real case id.
# 函数用途: 给小型真实验收 finding 附加 case_id。
def _case_extra(case: dict[str, Any]) -> dict[str, object]:
    return {"case_id": text(case.get("case_id"))}


__all__ = ["validate_small_real_acceptance_gate"]
