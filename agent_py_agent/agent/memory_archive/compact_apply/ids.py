
from __future__ import annotations

"""id helpers for non-destructive compact apply records."""

import hashlib
import json
from typing import Any


def compact_apply_plan_id(plan: dict[str, Any]) -> str:
    return "plan-" + _sha256_json(_identity_payload(plan))[:16]


def compact_apply_id(plan_id: str, now: str) -> str:
    return "apply-" + plan_id.removeprefix("plan-") + "-" + _safe_time_segment(now)


def compact_scope_hash(plan: dict[str, Any]) -> str:
    return _sha256_json({"workspace_root": plan["workspace_root"], "scope": plan["scope"]})[:16]


def compact_candidate_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "archive_records": int(plan["archive"]["record_count"]),
        "archive_files": int(plan["archive"]["file_count"]),
        "snapshot_files": int(plan["snapshots"]["file_count"]),
        "token_ledgers": int(plan["tokens"]["ledger_count"]),
    }


def compact_risk_level(plan: dict[str, Any]) -> str:
    if plan["risks"]:
        return "medium"
    if int(plan["snapshots"].get("invalid_count", 0) or 0) or int(plan["tokens"].get("invalid_count", 0) or 0):
        return "medium"
    return "low"


def _identity_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "candidate_counts": compact_candidate_counts(plan),
        "estimated_compactable_bytes": plan["estimated_compactable_bytes"],
        "risks": list(plan["risks"]),
    }


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_time_segment(value: str) -> str:
    return value.replace(":", "").replace("-", "").replace("+", "Z")


__all__ = [
    "compact_apply_id",
    "compact_apply_plan_id",
    "compact_candidate_counts",
    "compact_risk_level",
    "compact_scope_hash",
]
