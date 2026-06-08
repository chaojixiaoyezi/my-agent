
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .recovery import RecoveryAction
from .recovery import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload
from .tool_protocol_v2 import normalize_tool_call


@dataclass(frozen=True)
class ToolCallPolicy:
    available_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    required_parameters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    parameter_types: dict[str, dict[str, str]] = field(default_factory=dict)
    blocked_argument_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCallPolicyDecision:
    ok: bool
    tool_name: str
    error_code: str = ""
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "error_code": self.error_code,
            "findings": list(self.findings),
        }
        recovery = recovery_envelope_from_gate_payload(
            RecoveryEnvelopeRequest(
                gate="tool_call_policy",
                status="ALLOW" if self.ok else "NEED_REPAIR",
                allowed=self.ok,
                findings=_finding_payloads(self.error_code, self.findings),
                recommended_action=RecoveryAction.CONTINUE.value
                if self.ok
                else RecoveryAction.REPAIR_TOOL_CALL.value,
                evidence={"tool_name": self.tool_name} if self.tool_name else {},
            )
        )
        if recovery is not None:
            payload["recovery"] = recovery.to_dict()
        return payload


def validate_tool_call_policy(payload: Any, policy: ToolCallPolicy) -> ToolCallPolicyDecision:
    call = normalize_tool_call(payload)
    tool_name = call.tool_name
    if not tool_name:
        return _decision(False, tool_name, "TOOL_NAME_REQUIRED")
    available = _name_set(policy.available_tools)
    if available and tool_name not in available:
        return _decision(False, tool_name, "TOOL_NOT_FOUND")
    if tool_name in _name_set(policy.denied_tools):
        return _decision(False, tool_name, "TOOL_DENIED")
    allowed = _name_set(policy.allowed_tools)
    if allowed and tool_name not in allowed:
        return _decision(False, tool_name, "TOOL_NOT_ALLOWED")

    missing = _missing_required_parameters(tool_name, call.input, policy.required_parameters)
    if missing:
        return _decision(False, tool_name, "TOOL_PARAMETER_REQUIRED", missing)
    type_errors = _parameter_type_errors(tool_name, call.input, policy.parameter_types)
    if type_errors:
        return _decision(False, tool_name, "TOOL_PARAMETER_TYPE_INVALID", type_errors)
    blocked = _blocked_argument_matches(call.input, policy.blocked_argument_patterns)
    if blocked:
        return _decision(False, tool_name, "TOOL_PARAMETER_BLOCKED", blocked)
    return _decision(True, tool_name, "")


def _missing_required_parameters(
    tool_name: str,
    params: dict[str, Any],
    required: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    names = required.get(tool_name, ())
    return tuple(name for name in names if name not in params or params.get(name) is None)


def _parameter_type_errors(
    tool_name: str,
    params: dict[str, Any],
    schemas: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    errors: list[str] = []
    for name, expected in schemas.get(tool_name, {}).items():
        if name in params and not _matches_type(params[name], expected):
            errors.append(f"{name}:{expected}")
    return tuple(errors)


def _blocked_argument_matches(params: dict[str, Any], patterns: tuple[str, ...]) -> tuple[str, ...]:
    if not patterns:
        return ()
    findings: list[str] = []
    for path, value in _string_values(params):
        if any(re.search(pattern, value) for pattern in patterns):
            findings.append(path)
    return tuple(findings)


def _string_values(value: Any, prefix: str = "") -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((prefix or "$", value),)
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows.extend(_string_values(item, f"{prefix}.{key}" if prefix else str(key)))
        return tuple(rows)
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_string_values(item, f"{prefix}[{index}]"))
        return tuple(rows)
    return ()


def _matches_type(value: Any, expected: str) -> bool:
    kind = str(expected or "any").strip().lower()
    if kind in {"", "any"}:
        return True
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return False


def _name_set(names: tuple[str, ...]) -> set[str]:
    return {str(item).strip() for item in names if str(item).strip()}


def _decision(
    ok: bool,
    tool_name: str,
    error_code: str,
    findings: tuple[str, ...] = (),
) -> ToolCallPolicyDecision:
    return ToolCallPolicyDecision(ok=ok, tool_name=tool_name, error_code=error_code, findings=findings)


def _finding_payloads(error_code: str, findings: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    if not error_code:
        return ()
    evidence = {"fields": list(findings)} if findings else {}
    return ({"code": error_code, "evidence": evidence},)


__all__ = ["ToolCallPolicy", "ToolCallPolicyDecision", "validate_tool_call_policy"]
