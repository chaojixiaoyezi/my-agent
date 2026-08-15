
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class OfflinePlanValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_plan_contract(
    *,
    plan: dict[str, Any],
    contract: dict[str, Any],
    execution_events: tuple[dict[str, Any], ...] = (),
) -> OfflinePlanValidation:
    findings: list[dict[str, object]] = []
    _validate_output_paths(plan, contract, findings)
    _validate_required_tools(plan, contract, findings)
    _validate_forbidden_tools(plan, contract, findings)
    _validate_artifact_drift(plan, execution_events, findings)
    _validate_replan_changes_strategy(plan, execution_events, findings)
    return OfflinePlanValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_plan", findings),
    )


def _validate_output_paths(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    if _required_artifact_paths(contract) and not _string_tuple(plan.get("output_paths")):
        findings.append(_finding("PLAN_OUTPUT_PATH_MISSING"))


def _validate_required_tools(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    missing = sorted(set(_string_tuple(contract.get("required_tools"))) - set(_plan_tools(plan)))
    if missing:
        findings.append(_finding("PLAN_REQUIRED_TOOL_MISSING", {"tools": tuple(missing)}))


def _validate_forbidden_tools(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    used = set(_plan_tools(plan))
    forbidden_used = sorted(used & set(_string_tuple(contract.get("forbidden_tools"))))
    if forbidden_used:
        findings.append(_finding("PLAN_FORBIDDEN_TOOL", {"tools": tuple(forbidden_used)}))


def _validate_artifact_drift(
    plan: dict[str, Any],
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    allowed = set(_string_tuple(plan.get("output_paths")))
    if not allowed:
        return
    for event in events:
        if _text(event.get("type")) != "artifact_write":
            continue
        if _text(event.get("path")) not in allowed:
            findings.append(_finding("PLAN_ARTIFACT_DRIFT", {"path": _text(event.get("path"))}))
            return


def _validate_replan_changes_strategy(
    plan: dict[str, Any],
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    failed = {_tool_args_key(event) for event in events if _failed_tool_result(event)}
    planned = {_tool_args_key(step) for step in _plan_steps(plan)}
    repeated = sorted(key for key in planned & failed if key[0] and key[1])
    if repeated:
        findings.append(_finding("REPLAN_REPEATS_FAILED_PARAMS", {"tool": repeated[0][0]}))


def _required_artifact_paths(contract: dict[str, Any]) -> tuple[str, ...]:
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("required"), (list, tuple)):
        return ()
    return tuple(_text(item.get("path")) for item in artifacts["required"] if isinstance(item, dict) and item.get("path"))


def _plan_tools(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_text(step.get("tool")) for step in _plan_steps(plan) if _text(step.get("tool")))


def _plan_steps(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    steps = plan.get("steps")
    if not isinstance(steps, (list, tuple)):
        return ()
    return tuple(step for step in steps if isinstance(step, dict))


def _failed_tool_result(event: dict[str, Any]) -> bool:
    return _text(event.get("type")) == "tool_result" and event.get("ok") is False


def _tool_args_key(value: dict[str, Any]) -> tuple[str, str]:
    return (_text(value.get("tool")), _text(value.get("args_hash")))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (_text(item),) if text)


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}

__all__ = ["OfflinePlanValidation", "validate_plan_contract"]
