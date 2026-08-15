
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)

TRUSTED_TOOL_EXECUTOR_REFS = {
    "ToolExecutor.execute",
    "ToolRegistry.execute_tool",
    "tool_registry.execute_tool",
}
VALID_EFFECTS = {"read_only", "mutating", "dangerous"}
SIDE_EFFECT_EFFECTS = {"mutating", "dangerous"}


def validate_real_tool_dry_run_probes(probes: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    if not probes:
        findings.append(finding("REAL_TOOL_PROBES_MISSING"))
        return validation_report(findings)
    for probe in probes:
        _validate_one_probe(probe, findings)
    return validation_report(findings)


def _validate_one_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    _validate_probe_identity(probe, findings)
    effect = _validate_effect(probe, findings)
    _validate_executor_ref(probe, findings)
    _validate_result_schema(probe, findings)
    _validate_result_shape(probe, findings)
    if effect == "read_only":
        _validate_read_only_probe(probe, findings)
    if effect in SIDE_EFFECT_EFFECTS:
        _validate_side_effect_dry_run_probe(probe, findings)


def _validate_probe_identity(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(probe.get("probe_id")):
        findings.append(finding("REAL_TOOL_PROBE_ID_MISSING", _tool_extra(probe)))
    if not text(probe.get("operation_id")):
        findings.append(finding("REAL_TOOL_OPERATION_ID_MISSING", _tool_extra(probe)))
    if not text(probe.get("tool")):
        findings.append(finding("REAL_TOOL_NAME_MISSING", _tool_extra(probe)))


def _validate_effect(probe: dict[str, Any], findings: list[dict[str, object]]) -> str:
    effect = text(probe.get("effect"))
    if effect not in VALID_EFFECTS:
        findings.append(finding("REAL_TOOL_EFFECT_INVALID", _tool_extra(probe)))
        return ""
    return effect


def _validate_executor_ref(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    executor_ref = text(probe.get("tool_executor_ref"))
    if not executor_ref:
        findings.append(finding("REAL_TOOL_EXECUTOR_REF_MISSING", _tool_extra(probe)))
        return
    if executor_ref not in TRUSTED_TOOL_EXECUTOR_REFS:
        findings.append(finding("REAL_TOOL_EXECUTOR_REF_UNTRUSTED", _tool_extra(probe)))


def _validate_result_schema(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(probe.get("result_schema_ref")):
        findings.append(finding("REAL_TOOL_RESULT_SCHEMA_MISSING", _tool_extra(probe)))


def _validate_result_shape(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    result = probe.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        findings.append(finding("REAL_TOOL_RESULT_SHAPE_INVALID", _tool_extra(probe)))


def _validate_read_only_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(probe.get("mode")) != "read_only":
        findings.append(finding("REAL_TOOL_READ_ONLY_MODE_MISMATCH", _tool_extra(probe)))
    if _executed_actions(probe) or string_tuple(probe.get("side_effect_refs")) or _result_mode(probe) in {
        "apply",
        "execute",
        "real_run",
    }:
        findings.append(finding("REAL_TOOL_READ_ONLY_HAS_SIDE_EFFECTS", _tool_extra(probe)))


def _validate_side_effect_dry_run_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    result_mode = _result_mode(probe)
    if text(probe.get("mode")) != "dry_run" or result_mode != "dry_run":
        findings.append(finding("REAL_TOOL_DRY_RUN_MODE_MISMATCH", _tool_extra(probe)))
    if _executed_actions(probe) or _result_execution_flag(probe):
        findings.append(finding("REAL_TOOL_SIDE_EFFECT_EXECUTED", _tool_extra(probe)))
    if not text(probe.get("idempotency_key")):
        findings.append(finding("REAL_TOOL_IDEMPOTENCY_KEY_MISSING", _tool_extra(probe)))
    if not text(probe.get("args_hash")):
        findings.append(finding("REAL_TOOL_ARGS_HASH_MISSING", _tool_extra(probe)))


def _result_mode(probe: dict[str, Any]) -> str:
    result = probe.get("result")
    if not isinstance(result, dict):
        return ""
    payload = _payload(result)
    return text(payload.get("mode") or result.get("mode"))


def _result_execution_flag(probe: dict[str, Any]) -> bool:
    result = probe.get("result")
    if not isinstance(result, dict):
        return False
    payload = _payload(result)
    return any(
        value is True
        for value in (
            result.get("executed"),
            result.get("real_execution"),
            payload.get("executed"),
            payload.get("real_execution"),
        )
    )


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else result


def _executed_actions(probe: dict[str, Any]) -> tuple[str, ...]:
    return string_tuple(probe.get("executed_actions"))


def _tool_extra(probe: dict[str, Any]) -> dict[str, object]:
    return {
        "tool": text(probe.get("tool")),
        "probe_id": text(probe.get("probe_id")),
        "operation_id": text(probe.get("operation_id")),
    }


__all__ = [
    "TRUSTED_TOOL_EXECUTOR_REFS",
    "validate_real_tool_dry_run_probes",
]
