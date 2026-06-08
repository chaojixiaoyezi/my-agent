
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..recovery import RecoveryAction
from ..recovery import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload

GateValidator = Callable[["GateContext"], "GateDecision"]


@dataclass(frozen=True)
class GateFinding:
    code: str
    severity: str = "P1"
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class GateContext:
    phase: str
    payload: Any = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateDecision:
    gate: str
    status: str
    allowed: bool
    findings: tuple[GateFinding, ...] = ()
    recommended_action: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def allow_action(self) -> bool:
        """Return whether the current attempted action may run."""
        return self.allowed

    @property
    def block_task(self) -> bool:
        """Return True only for explicit terminal-task decisions."""
        return self.evidence.get("block_task") is True

    @property
    def severity(self) -> str:
        """Classify the gate for operators without changing its status semantics."""
        if self.allowed and not self.findings:
            return "info"
        if self.gate in _SAFETY_GATES or any(_safety_code(item.code) for item in self.findings):
            return "safety"
        if self.gate in _QUALITY_GATES or self.status == "NEED_REPAIR":
            return "quality"
        return "engineering"

    @property
    def model_message(self) -> str:
        """Short model-facing repair or steering message."""
        if self.allowed and not self.findings:
            return ""
        messages = [finding.message for finding in self.findings if finding.message]
        if messages:
            return "；".join(messages)[:500]
        codes = ", ".join(self.finding_codes)
        if self.allowed:
            return f"{self.gate} 提醒：{codes}"
        return f"{self.gate} 未放行当前动作：{codes}"

    @property
    def operator_message(self) -> str:
        """Compact operator/debug summary."""
        codes = ",".join(self.finding_codes)
        return f"{self.gate}:{self.status}:{codes}".rstrip(":")

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        """Extract path-like references from structured evidence fields."""
        return tuple(_evidence_refs(self.evidence))

    @property
    def finding_codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)

    @classmethod
    def allow(
        cls,
        gate: str,
        *,
        recommended_action: str = RecoveryAction.CONTINUE.value,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "ALLOW", True, recommended_action=recommended_action, evidence=evidence or {})

    @classmethod
    def deny(
        cls,
        gate: str,
        code: str,
        *,
        evidence: dict[str, Any] | None = None,
        recommended_action: str = RecoveryAction.STOP.value,
    ) -> GateDecision:
        return cls(
            gate,
            "DENY",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            recommended_action=recommended_action,
        )

    @classmethod
    def repair(
        cls,
        gate: str,
        findings: list[GateFinding] | tuple[GateFinding, ...],
        *,
        recommended_action: str = RecoveryAction.REPAIR.value,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "NEED_REPAIR", False, tuple(findings), recommended_action, evidence or {})

    @classmethod
    def need_approval(
        cls,
        gate: str,
        code: str = "APPROVAL_REQUIRED",
        *,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(
            gate,
            "NEED_APPROVAL",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            RecoveryAction.REQUEST_APPROVAL.value,
            {},
        )

    @classmethod
    def block(
        cls,
        gate: str,
        code: str,
        *,
        evidence: dict[str, Any] | None = None,
        recommended_action: str = RecoveryAction.REPORT_BLOCKER.value,
    ) -> GateDecision:
        return cls(
            gate,
            "BLOCKED",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            recommended_action,
            {},
        )

    @classmethod
    def recovering(
        cls,
        gate: str,
        findings: list[GateFinding] | tuple[GateFinding, ...],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "RECOVERING", False, tuple(findings), RecoveryAction.RECOVER_FROM_CHECKPOINT.value, evidence or {})

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "gate": self.gate,
            "status": self.status,
            "allowed": self.allowed,
            "allow_action": self.allow_action,
            "block_task": self.block_task,
            "severity": self.severity,
            "model_message": self.model_message,
            "operator_message": self.operator_message,
            "evidence_refs": list(self.evidence_refs),
            "findings": [finding.to_dict() for finding in self.findings],
            "recommended_action": self.recommended_action,
            "evidence": dict(self.evidence),
        }
        recovery = recovery_envelope_from_gate_payload(
            RecoveryEnvelopeRequest(
                gate=self.gate,
                status=self.status,
                allowed=self.allowed,
                findings=payload["findings"],
                recommended_action=self.recommended_action,
                evidence=payload["evidence"],
            )
        )
        if recovery is not None:
            payload["recovery"] = recovery.to_dict()
        return payload


_SAFETY_GATES = {
    "approval_binding",
    "idempotency_ledger",
    "network_safety",
    "path_url_command",
    "skill_guard",
    "tool_effect",
    "tool_manifest",
}

_QUALITY_GATES = {
    "artifact_gate",
    "artifact_provenance",
    "artifact_report",
    "delivery_closeout",
    "delivery_quality",
    "document_content_quality",
    "fact_evidence",
    "final_closeout",
}

_SAFETY_CODE_PREFIXES = (
    "APPROVAL_",
    "DANGEROUS_",
    "IDEMPOTENCY_",
    "NETWORK_",
    "PATH_OUTSIDE_",
    "SECURITY_",
    "TOOL_NOT_",
)


def _safety_code(code: str) -> bool:
    text = str(code or "").upper()
    return any(text.startswith(prefix) for prefix in _SAFETY_CODE_PREFIXES)


def _evidence_refs(value: object) -> list[str]:
    refs: list[str] = []
    for key, item in _walk_evidence(value):
        if _ref_key(key):
            _append_ref_value(item, refs)
    return _dedupe_refs(refs)


def _walk_evidence(value: object) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    stack: list[object] = [value]
    while stack:
        _walk_evidence_item(stack.pop(), rows, stack)
    return rows


def _walk_evidence_item(item: object, rows: list[tuple[str, object]], stack: list[object]) -> None:
    if isinstance(item, dict):
        rows.extend((str(key), val) for key, val in item.items())
        stack.extend(val for val in item.values() if isinstance(val, dict | list | tuple))
    elif isinstance(item, list | tuple):
        stack.extend(item)


def _append_ref_value(value: object, refs: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            refs.append(text)
        return
    if isinstance(value, list | tuple | set):
        for item in value:
            _append_ref_value(item, refs)


def _ref_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.endswith("_ref") or normalized.endswith("_refs") or normalized in {"path", "paths"}


def _dedupe_refs(refs: list[str]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        if ref not in result:
            result.append(ref)
    return result


__all__ = ["GateContext", "GateDecision", "GateFinding", "GateValidator"]
