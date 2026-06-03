
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    positive_int,
    text,
    validation_report,
)

REQUIRED_TRIAL_REFS = ("prompt_ref", "response_ref", "tool_trace_ref", "contract_hash")


def validate_live_llm_fake_tool_trial(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    refs = facts.get("refs") if isinstance(facts.get("refs"), dict) else {}
    metrics = facts.get("metrics") if isinstance(facts.get("metrics"), dict) else {}
    _validate_trial_shape(facts, refs, findings)
    _validate_completion_claim(facts, findings)
    _validate_metrics(metrics, findings)
    return validation_report(findings)


def _validate_trial_shape(
    facts: dict[str, Any],
    refs: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    missing = tuple(name for name in REQUIRED_TRIAL_REFS if not text(refs.get(name)))
    if missing:
        findings.append(finding("LIVE_LLM_TRIAL_REF_MISSING", {"refs": missing}))
    if facts.get("real_llm") is not True or text(facts.get("tool_mode")) != "fake":
        findings.append(finding("LIVE_LLM_TRIAL_MODE_INVALID"))


def _validate_completion_claim(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    claim = facts.get("final_claim") if isinstance(facts.get("final_claim"), dict) else {}
    verifier = facts.get("verifier") if isinstance(facts.get("verifier"), dict) else {}
    if text(claim.get("status")).lower() == "succeeded" and verifier.get("ok") is not True:
        findings.append(finding("LIVE_LLM_FAKE_COMPLETION_REJECTED"))


def _validate_metrics(metrics: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if positive_int(metrics.get("unknown_tool_count")):
        findings.append(finding("LIVE_LLM_UNKNOWN_TOOL"))
    if positive_int(metrics.get("schema_error_count")):
        findings.append(finding("LIVE_LLM_TOOL_SCHEMA_ERROR"))
    if metrics.get("tool_failure_claimed_success") is True:
        findings.append(finding("LIVE_LLM_TOOL_FAILURE_CLAIMED_SUCCESS"))
    if metrics.get("dry_run_claimed_real") is True:
        findings.append(finding("LIVE_LLM_DRY_RUN_CLAIMED_REAL"))


__all__ = ["validate_live_llm_fake_tool_trial"]
