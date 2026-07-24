
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..recovery import RecoveryAction
from .models import GateDecision, GateFinding

VALID_TOOL_EFFECTS = {"read_only", "mutating", "dangerous"}
SIDE_EFFECT_TOOL_EFFECTS = {"mutating", "dangerous"}
VALID_IDEMPOTENCY_SCOPES = {"operation", "business"}


@dataclass(frozen=True)
class ToolManifestFacts:
    tool_name: str
    effect: object = ""
    parameters: object = None
    mode: object = ""
    idempotency_scope: str = ""
    requires_approval: bool = False
    timeout_seconds: object = 0
    output_refs: object = None


def evaluate_tool_manifest_gate(facts: ToolManifestFacts | Mapping[str, object]) -> GateDecision:
    item = _manifest_facts(facts)
    findings: list[GateFinding] = []
    name = str(item.tool_name or "").strip()
    effect = str(item.effect or "").strip().lower()
    if not name:
        findings.append(GateFinding("TOOL_MANIFEST_NAME_MISSING"))
    if not effect:
        findings.append(GateFinding("TOOL_MANIFEST_EFFECT_MISSING", evidence={"tool_name": name}))
    elif effect not in VALID_TOOL_EFFECTS:
        findings.append(GateFinding("TOOL_MANIFEST_EFFECT_INVALID", evidence={"tool_name": name, "effect": effect}))
    if not isinstance(item.parameters, Mapping):
        findings.append(GateFinding("TOOL_MANIFEST_SCHEMA_MISSING", evidence={"tool_name": name}))
    if (
        isinstance(item.parameters, Mapping)
        and effect in SIDE_EFFECT_TOOL_EFFECTS
        and str(item.idempotency_scope or "").strip().lower()
        not in VALID_IDEMPOTENCY_SCOPES
    ):
        findings.append(GateFinding("TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING", evidence={"tool_name": name}))
    timeout = _int_or_zero(item.timeout_seconds)
    if timeout < 0:
        findings.append(GateFinding("TOOL_MANIFEST_TIMEOUT_INVALID", evidence={"tool_name": name, "timeout": timeout}))
    if findings:
        return GateDecision(
            "tool_manifest",
            "DENY",
            False,
            tuple(findings),
            RecoveryAction.CHANGE_STRATEGY.value,
            {},
        )
    return GateDecision.allow(
        "tool_manifest",
        evidence={
            "tool_name": name,
            "effect": effect,
            "parameter_count": len(item.parameters or {}),
            "idempotency_scope": str(item.idempotency_scope or ""),
            "requires_approval": bool(item.requires_approval),
            "timeout_seconds": timeout,
        },
    )


def tool_manifest_from_spec(spec: object) -> ToolManifestFacts:
    effect = getattr(spec, "effect", "")
    return ToolManifestFacts(
        tool_name=str(getattr(spec, "name", "") or ""),
        effect=effect,
        parameters=getattr(spec, "parameters", None),
        mode=getattr(spec, "default_mode", ""),
        idempotency_scope=str(getattr(spec, "idempotency_scope", "") or ""),
        requires_approval=bool(getattr(spec, "requires_approval", False)),
        timeout_seconds=getattr(spec, "timeout_seconds", 0),
        output_refs=getattr(spec, "output_refs", None),
    )


def _manifest_facts(value: ToolManifestFacts | Mapping[str, object]) -> ToolManifestFacts:
    if isinstance(value, ToolManifestFacts):
        return value
    return ToolManifestFacts(
        tool_name=str(value.get("tool_name") or ""),
        effect=value.get("effect", ""),
        parameters=value.get("parameters") if "parameters" in value else value.get("input_schema"),
        mode=value.get("mode", ""),
        idempotency_scope=str(value.get("idempotency_scope", "") or ""),
        requires_approval=bool(value.get("requires_approval", False)),
        timeout_seconds=value.get("timeout_seconds", 0),
        output_refs=value.get("output_refs"),
    )


def _int_or_zero(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


__all__ = [
    "SIDE_EFFECT_TOOL_EFFECTS",
    "ToolManifestFacts",
    "VALID_TOOL_EFFECTS",
    "evaluate_tool_manifest_gate",
    "tool_manifest_from_spec",
]
