# LLM: Contract validation recovery gives non-runtime validators the same repair envelope shape as gates.
# 模块用途: 为离线合同、配置合同和 trace 合同生成统一 recovery；中文只作可读提示，不作机器事实。

from __future__ import annotations

from typing import Any

from .recovery_envelope import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload


def recovery_for_findings(gate: str, findings: tuple[dict[str, object], ...] | list[dict[str, object]]) -> dict[str, object] | None:
    if not findings:
        return None
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate=gate,
            status="NEED_REPAIR",
            allowed=False,
            findings=tuple(_finding(item) for item in findings),
            recommended_action="repair_against_contract_findings",
            evidence={"finding_count": len(findings)},
        )
    )
    return envelope.to_dict() if envelope is not None else None


def _finding(item: dict[str, object]) -> dict[str, object]:
    evidence = {key: value for key, value in item.items() if key not in {"code", "severity", "message"}}
    return {
        "code": str(item.get("code") or "CONTRACT_FINDING"),
        "severity": str(item.get("severity") or "P1"),
        "message": str(item.get("message") or ""),
        "evidence": evidence,
    }


__all__ = ["recovery_for_findings"]
