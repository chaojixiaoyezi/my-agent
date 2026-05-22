# LLM: Artifact gates enforce refs and acceptance findings at delivery closeout.
# 模块用途: 校验产物 ref/kind、acceptance_report 和交付报告，不接受口头完成替代机器验收。

from __future__ import annotations

from typing import Any

from .artifact_provenance import evaluate_artifact_provenance_gate
from .models import GateDecision, GateFinding


# LLM: evaluate_delivery_closeout_gate checks all required delivery artifacts.
# 函数用途: 在主代理收口前确认报告 ref 存在、产物列表存在且每个产物验收报告通过。
def evaluate_delivery_closeout_gate(report: dict[str, Any]) -> GateDecision:
    if not str(report.get("report_ref") or "").strip():
        return GateDecision.deny("delivery_closeout", "CLOSEOUT_REPORT_REF_MISSING")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return GateDecision.deny("delivery_closeout", "CLOSEOUT_ARTIFACTS_MISSING")
    run_id = str(report.get("run_id") or "").strip()
    failed = [_artifact_item_gate(item, run_id=run_id) for item in artifacts if isinstance(item, dict)]
    failed = [decision for decision in failed if not decision.allowed]
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


# LLM: evaluate_artifact_report_gate validates one artifact acceptance report.
# 函数用途: 要求产物有 ref/kind，并把 hard/P0/P1 finding 转成 NEED_REPAIR。
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


# LLM: _artifact_item_gate normalizes delivery artifact rows into artifact reports.
# 函数用途: 把 closeout artifact item 的 path/kind/ok 补到 acceptance_report 后再复用产物门。
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


# LLM: _with_artifact_context preserves artifact identity on failed finding rows.
# 函数用途: 给每条产物 finding 补 artifact_id/path，方便父级修复定位具体产物。
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


# LLM: _artifact_findings converts artifact validator findings into GateFinding rows.
# 函数用途: 只读取 acceptance_report.findings 结构字段，不扫描产物正文或报告文本。
def _artifact_findings(report: dict[str, Any]) -> list[GateFinding]:
    raw = report.get("findings")
    if not isinstance(raw, list):
        return []
    return [_artifact_finding(item) for item in raw if isinstance(item, dict)]


# LLM: _artifact_finding keeps artifact validator diagnostics stable.
# 函数用途: 将 code/severity/message/location/value 字段映射到统一 gate finding。
def _artifact_finding(item: dict[str, Any]) -> GateFinding:
    return GateFinding(
        str(item.get("code") or "ARTIFACT_FINDING"),
        severity=str(item.get("severity") or "hard"),
        message=str(item.get("message") or ""),
        evidence={"location": str(item.get("location") or ""), "value": str(item.get("value") or "")},
    )


__all__ = ["evaluate_artifact_report_gate", "evaluate_delivery_closeout_gate"]
