
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    string_tuple,
    text,
    validation_report,
)

SIDE_EFFECTS = {"mutating", "dangerous"}
REVIEW_ONLY_MODES = {"", "recommend", "draft", "dry_run"}


def validate_shadow_mode_run(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_mode(facts, findings)
    _validate_risk(facts, findings)
    _validate_evidence(facts, findings)
    _validate_recommendations(facts, findings)
    _validate_no_real_execution(facts, findings)
    _validate_human_review(facts, findings)
    return validation_report(findings)


def _validate_mode(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(facts.get("mode")) != "shadow":
        findings.append(finding("SHADOW_MODE_INVALID"))


def _validate_risk(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    risk = facts.get("risk") if isinstance(facts.get("risk"), dict) else {}
    score = _number(risk.get("score"))
    if score is None or score < 0 or score > 100:
        findings.append(finding("SHADOW_RISK_SCORE_MISSING"))


def _validate_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    evidence = dict_items(facts.get("evidence_refs"))
    if not evidence:
        findings.append(finding("SHADOW_EVIDENCE_SOURCE_MISSING"))
        return
    for item in evidence:
        if text(item.get("source_type")) and text(item.get("source_ref")):
            continue
        findings.append(finding("SHADOW_EVIDENCE_SOURCE_MISSING", {"evidence_id": text(item.get("evidence_id"))}))
        return


def _validate_recommendations(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    dry_run_ids = _successful_dry_run_action_ids(facts)
    for action in dict_items(facts.get("recommended_actions")):
        _validate_action_mode(action, findings)
        _validate_operator_review(action, findings)
        if text(action.get("effect")) == "dangerous":
            _validate_dangerous_action(action, dry_run_ids, findings)


def _validate_action_mode(action: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(action.get("mode")) not in REVIEW_ONLY_MODES:
        findings.append(finding("SHADOW_ACTION_NOT_REVIEW_ONLY", {"action_id": text(action.get("action_id"))}))


def _validate_operator_review(action: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(action.get("effect")) in SIDE_EFFECTS and not text(action.get("operator_review_ref")):
        findings.append(finding("SHADOW_OPERATOR_REVIEW_REF_MISSING", {"action_id": text(action.get("action_id"))}))


def _validate_dangerous_action(
    action: dict[str, Any],
    dry_run_ids: set[str],
    findings: list[dict[str, object]],
) -> None:
    action_id = text(action.get("action_id"))
    if action_id not in dry_run_ids:
        findings.append(finding("SHADOW_DANGEROUS_DRY_RUN_MISSING", {"action_id": action_id}))
    if not text(action.get("approval_draft_ref")):
        findings.append(finding("SHADOW_APPROVAL_DRAFT_MISSING", {"action_id": action_id}))


def _validate_no_real_execution(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if dict_items(facts.get("executed_actions")):
        findings.append(finding("SHADOW_REAL_ACTION_EXECUTED"))


def _validate_human_review(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    review = facts.get("human_review") if isinstance(facts.get("human_review"), dict) else {}
    if not review:
        findings.append(finding("SHADOW_HUMAN_REVIEW_MISSING"))
        return
    if not (text(review.get("review_id")) and text(review.get("review_ref")) and text(review.get("decision"))):
        findings.append(finding("SHADOW_HUMAN_REVIEW_INCOMPLETE"))
    if review.get("agreement") is False and not _review_disagreement_reasons(review):
        findings.append(finding("SHADOW_REVIEW_DISAGREEMENT_REASON_MISSING"))


def _successful_dry_run_action_ids(facts: dict[str, Any]) -> set[str]:
    return {
        text(item.get("action_id"))
        for item in dict_items(facts.get("dry_run_results"))
        if text(item.get("mode")) == "dry_run" and item.get("ok") is True and text(item.get("result_ref"))
    }


def _review_disagreement_reasons(review: dict[str, Any]) -> tuple[str, ...]:
    return string_tuple(review.get("mismatch_reason_codes")) + string_tuple(review.get("missing_evidence_codes"))


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["validate_shadow_mode_run"]
