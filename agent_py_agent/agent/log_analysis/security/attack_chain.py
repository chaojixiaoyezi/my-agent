from __future__ import annotations

"""Attack-chain draft generation from soft findings."""

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from ..models import CaseRecord, EvidenceRef, Finding


@dataclass
class AttackChainStep:
    time: str
    stage: str
    action: str
    entities: dict[str, list[str]] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    basis: str = "finding"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_attack_chain(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[AttackChainStep]:
    findings = sorted(_extract_findings(case_or_findings), key=lambda item: item.window[0] if item.window else "")
    steps: list[AttackChainStep] = []
    for finding in findings:
        stage, action = _stage_for_detector(finding.detector_id)
        steps.append(
            AttackChainStep(
                time=finding.window[0] if finding.window else "",
                stage=stage,
                action=action,
                entities=finding.entities,
                evidence_refs=_ref_ids(finding.evidence_refs),
                confidence=finding.confidence if finding.confidence is not None else finding.risk_score,
            )
        )
    return steps


def lateral_movement_signs(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[dict[str, Any]]:
    signs: list[dict[str, Any]] = []
    for finding in _extract_findings(case_or_findings):
        targets = _entity_values(finding.entities, "victim_ip", "dst_ip", "host")
        if finding.detector_id in {"bruteforce_then_success", "vpn_new_geo_login"}:
            signs.append(
                {
                    "kind": "identity_to_asset_access",
                    "summary": finding.hypothesis,
                    "entities": finding.entities,
                    "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
                    "evidence_refs": _ref_ids(finding.evidence_refs),
                }
            )
        if len(targets) >= 3:
            signs.append(
                {
                    "kind": "multi_target_activity",
                    "summary": "The finding touches multiple target assets in one window.",
                    "targets": targets,
                    "confidence": min(0.75, finding.confidence if finding.confidence is not None else finding.risk_score),
                    "evidence_refs": _ref_ids(finding.evidence_refs),
                }
            )
    return signs


def _stage_for_detector(detector_id: str) -> tuple[str, str]:
    mapping = {
        "waf_attack_success_candidate": ("initial_access", "Web exploit candidate with post-alert server behavior."),
        "web_to_process_anomaly": ("execution", "Web service launched an unusual child process."),
        "vpn_new_geo_login": ("initial_access", "VPN login used novel or unusual source context."),
        "bruteforce_then_success": ("credential_access", "Repeated failed authentication was followed by success."),
        "rare_egress_after_alert": ("command_and_control", "Alerted asset reached a rare external destination."),
        "multi_source_weak_signal": ("correlation", "Multiple weak signals overlapped on one entity."),
    }
    return mapping.get(detector_id, ("unknown", detector_id or "unknown finding"))


def _extract_findings(value: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[Finding]:
    if isinstance(value, CaseRecord):
        attributes = value.attributes if isinstance(value.attributes, Mapping) else {}
        return [Finding.from_dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        if "findings" in value:
            return [Finding.from_dict(item) for item in value.get("findings", [])]
        attributes = value.get("attributes")
        if isinstance(attributes, Mapping) and "finding_summaries" in attributes:
            return [Finding.from_dict(item) for item in attributes.get("finding_summaries", [])]
        return [Finding.from_dict(value)]
    return [item if isinstance(item, Finding) else Finding.from_dict(item) for item in value]


def _entity_values(entities: Mapping[str, Sequence[Any]], *keys: str) -> list[str]:
    result: list[str] = []
    for key in keys:
        for value in entities.get(key, []):
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        if isinstance(ref, EvidenceRef):
            result.append(ref.evidence_id)
        elif isinstance(ref, Mapping):
            result.append(str(ref.get("evidence_id") or ref.get("raw_ref") or ref))
        else:
            result.append(str(ref))
    return result


__all__ = ["AttackChainStep", "build_attack_chain", "lateral_movement_signs"]
