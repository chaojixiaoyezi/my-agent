
from __future__ import annotations

from typing import Any

from ..models import GateDecision, GateFinding
from .provenance import evaluate_artifact_provenance_gate


def evaluate_delivery_closeout_gate(report: dict[str, Any]) -> GateDecision:
    if not str(report.get("report_ref") or "").strip():
        return GateDecision.deny("delivery_closeout", "CLOSEOUT_REPORT_REF_MISSING")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return GateDecision.deny("delivery_closeout", "CLOSEOUT_ARTIFACTS_MISSING")
    run_id = str(report.get("run_id") or "").strip()
    failed = [_artifact_item_gate(item, run_id=run_id) for item in artifacts if isinstance(item, dict)]
    failed = [decision for decision in failed if not decision.allowed]
    coverage_decision = _target_coverage_gate(report)
    if not coverage_decision.allowed:
        failed.append(coverage_decision)
    if report.get("ok") is not True or failed:
        findings = [finding for decision in failed for finding in decision.findings]
        return GateDecision.repair(
            "delivery_closeout",
            findings or [GateFinding("DELIVERY_CONTRACT_FAILED")],
            evidence={"failed_count": len(failed)},
        )
    return GateDecision.allow(
        "delivery_closeout",
        evidence={"artifact_count": len(artifacts), "report_ref": str(report.get("report_ref") or "")},
    )


def _target_coverage_gate(report: dict[str, Any]) -> GateDecision:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return GateDecision.allow("target_coverage", evidence={"skipped": "not_required_or_complete"})
    return GateDecision.repair(
        "target_coverage",
        [
            GateFinding(
                "TARGET_COVERAGE_MISSING",
                evidence={
                    "scope_label": str(status.get("scope_label") or ""),
                    "enforcement": str(status.get("enforcement") or ""),
                    "missing_count": int(status.get("missing_count") or 0),
                    "missing_items": list(status.get("missing_items") or [])[:20],
                },
            )
        ],
        evidence={
            "expected_count": int(status.get("expected_count") or 0),
            "covered_count": int(status.get("covered_count") or 0),
            "missing_count": int(status.get("missing_count") or 0),
        },
    )


def evaluate_artifact_report_gate(report: dict[str, Any]) -> GateDecision:
    artifact_ref = str(report.get("artifact_ref") or report.get("path") or "").strip()
    if not artifact_ref:
        return GateDecision.deny("artifact_report", "ARTIFACT_REF_MISSING")
    if str(report.get("artifact_kind") or report.get("kind") or "").strip() == "":
        return GateDecision.deny("artifact_report", "ARTIFACT_KIND_MISSING", evidence={"artifact_ref": artifact_ref})
    findings = _artifact_findings(report)
    hard = [finding for finding in findings if str(finding.severity).lower() in {"hard", "p0", "p1"}]
    if report.get("ok") is not True or hard:
        return GateDecision.repair("artifact_report", hard or findings or [GateFinding("ARTIFACT_ACCEPTANCE_FAILED")])
    return GateDecision.allow("artifact_report", evidence={"artifact_ref": artifact_ref})


def _artifact_item_gate(item: dict[str, Any], *, run_id: str = "") -> GateDecision:
    report = item.get("acceptance_report")
    payload = dict(report) if isinstance(report, dict) else {}
    payload.setdefault("ok", item.get("ok"))
    payload.setdefault("artifact_ref", item.get("path"))
    payload.setdefault("artifact_kind", item.get("kind"))
    decision = evaluate_artifact_report_gate(payload)
    provenance_decision = evaluate_artifact_provenance_gate(item, run_id=run_id)
    failed = [child for child in (decision, provenance_decision) if not child.allowed]
    if not failed:
        return decision
    findings = [finding for child in failed for finding in child.findings]
    return _with_artifact_context(
        item,
        GateDecision.repair(
            "artifact_report",
            findings,
            evidence={
                "artifact_report_allowed": decision.allowed,
                "artifact_provenance_allowed": provenance_decision.allowed,
            },
        ),
    )


def _with_artifact_context(item: dict[str, Any], decision: GateDecision) -> GateDecision:
    return GateDecision.repair(
        "artifact_report",
        [
            GateFinding(
                finding.code,
                severity=finding.severity,
                message=finding.message,
                evidence={**finding.evidence, "artifact_id": str(item.get("artifact_id") or ""), "path": str(item.get("path") or "")},
            )
            for finding in decision.findings
        ],
        evidence=decision.evidence,
    )


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


__all__ = ["evaluate_artifact_report_gate", "evaluate_delivery_closeout_gate"]
