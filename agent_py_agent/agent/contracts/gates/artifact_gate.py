
from __future__ import annotations

from typing import Any

from .models import GateDecision, GateFinding


def evaluate_artifact_report_gate(report: dict[str, Any]) -> GateDecision:
    artifact_ref = str(report.get("artifact_ref") or "").strip()
    if not artifact_ref:
        return GateDecision.deny("artifact_report", "ARTIFACT_REF_MISSING")
    if str(report.get("artifact_kind") or "").strip() == "":
        return GateDecision.deny("artifact_report", "ARTIFACT_KIND_MISSING", evidence={"artifact_ref": artifact_ref})
    findings = _artifact_findings(report)
    hard = [finding for finding in findings if str(finding.severity).lower() in {"hard", "p0", "p1"}]
    if report.get("ok") is not True or hard:
        return GateDecision.repair("artifact_report", hard or findings or [GateFinding("ARTIFACT_ACCEPTANCE_FAILED")])
    return GateDecision.allow("artifact_report", evidence={"artifact_ref": artifact_ref})


def _artifact_findings(report: dict[str, Any]) -> list[GateFinding]:
    raw = report.get("findings")
    if not isinstance(raw, list):
        return []
    return [_artifact_finding(item) for item in raw if isinstance(item, dict)]


def _artifact_finding(item: dict[str, Any]) -> GateFinding:
    return GateFinding(
        str(item.get("code") or "ARTIFACT_FINDING"),
        severity=str(item.get("severity") or "hard"),
        message=str(item.get("message") or ""),
        evidence={"location": str(item.get("location") or ""), "value": str(item.get("value") or "")},
    )


__all__ = ["evaluate_artifact_report_gate"]
