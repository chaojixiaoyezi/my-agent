
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)

READ_ONLY_REQUIRED_TESTS = ("success", "timeout", "auth_failure", "empty_result", "large_output", "secret_redaction")
SIDE_EFFECTS = {"mutating", "dangerous"}
VALID_EFFECTS = {"read_only", "mutating", "dangerous"}


def validate_tool_adapter_readiness(adapters: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    for adapter in adapters:
        _validate_one_adapter(adapter, findings)
    return validation_report(findings)


def _validate_one_adapter(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    effect = text(adapter.get("effect"))
    if effect not in VALID_EFFECTS:
        findings.append(finding("ADAPTER_EFFECT_MISSING", {"tool": text(adapter.get("tool"))}))
    if not text(adapter.get("result_schema_ref")):
        findings.append(finding("ADAPTER_RESULT_SCHEMA_MISSING", {"tool": text(adapter.get("tool"))}))
    if effect == "read_only":
        _validate_read_only_tests(adapter, findings)
    if "dry_run" in string_tuple(adapter.get("modes")) or effect in SIDE_EFFECTS:
        _validate_dry_run_mode_field(adapter, findings)
    if effect in SIDE_EFFECTS:
        _validate_side_effect_adapter(adapter, findings)
    if "dry_run" in string_tuple(adapter.get("modes")) or effect in SIDE_EFFECTS:
        _validate_dry_run_only(adapter, findings)


def _validate_read_only_tests(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    missing = tuple(name for name in READ_ONLY_REQUIRED_TESTS if name not in set(string_tuple(adapter.get("contract_tests"))))
    if missing:
        findings.append(finding("ADAPTER_READ_ONLY_TEST_COVERAGE_MISSING", {"tool": text(adapter.get("tool"))}))


def _validate_dry_run_mode_field(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(adapter.get("mode_field")) and "dry_run" in string_tuple(adapter.get("modes")):
        findings.append(finding("ADAPTER_DRY_RUN_MODE_FIELD_MISSING", {"tool": text(adapter.get("tool"))}))


def _validate_dry_run_only(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if adapter.get("dry_run_only") is False and "real_run" in string_tuple(adapter.get("modes")):
        findings.append(finding("ADAPTER_DRY_RUN_ONLY_VIOLATED", {"tool": text(adapter.get("tool"))}))


def _validate_side_effect_adapter(adapter: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if adapter.get("real_execution_enabled") is True and adapter.get("requires_approval") is not True:
        findings.append(finding("ADAPTER_REAL_RUN_NOT_APPROVED", {"tool": text(adapter.get("tool"))}))
    if adapter.get("idempotency_key_required") is not True:
        findings.append(finding("ADAPTER_SIDE_EFFECT_IDEMPOTENCY_MISSING", {"tool": text(adapter.get("tool"))}))


__all__ = ["READ_ONLY_REQUIRED_TESTS", "validate_tool_adapter_readiness"]
