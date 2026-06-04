
from __future__ import annotations

from typing import Any

_SCOPE_FIELDS = ("request_id", "run_id", "task_id")


def compact_context_bundle_match(plan_scope: dict[str, Any], bundle: dict[str, Any], *, explicit: bool) -> dict[str, Any]:
    ref = str(bundle.get("ref", "") or "")
    if not ref:
        return _report("no_context_bundle", explicit=explicit, ref="", mismatches=[])
    if not bundle.get("loaded"):
        return _report(str(bundle.get("error") or "context_bundle_not_loaded"), explicit=explicit, ref=ref, mismatches=[])
    plan = _scope_values(plan_scope)
    candidate = _scope_values(bundle.get("scope", {}) if isinstance(bundle.get("scope"), dict) else {})
    comparable = {key: value for key, value in plan.items() if value}
    if not comparable:
        return _report("explicit_no_plan_scope" if explicit else "unchecked_no_plan_scope", explicit=explicit, ref=ref, mismatches=[])
    mismatches = [
        {"field": key, "expected": value, "actual": candidate.get(key, "")}
        for key, value in comparable.items()
        if candidate.get(key, "") and candidate.get(key, "") != value
    ]
    if mismatches:
        return _report("explicit_scope_mismatch" if explicit else "scope_mismatch", explicit=explicit, ref=ref, mismatches=mismatches)
    return _report("matched", explicit=explicit, ref=ref, mismatches=[])


def context_bundle_match_allows_attach(report: dict[str, Any]) -> bool:
    return str(report.get("status") or "") in {"matched", "explicit_scope_mismatch", "explicit_no_plan_scope", "unchecked_no_plan_scope"}


def _scope_values(scope: dict[str, Any]) -> dict[str, str]:
    return {field: str(scope.get(field) or "").strip() for field in _SCOPE_FIELDS}


def _report(status: str, *, explicit: bool, ref: str, mismatches: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "status": status,
        "explicit_ref": bool(explicit),
        "candidate_ref": ref,
        "mismatches": mismatches,
    }


__all__ = ["compact_context_bundle_match", "context_bundle_match_allows_attach"]
