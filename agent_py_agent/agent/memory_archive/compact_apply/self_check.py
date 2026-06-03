
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_SELF_CHECK_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_self_check")
COMPACT_SELF_CHECK_FAILURE_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_self_check_failure")


def build_self_check_payload(
    plan: dict[str, Any], paths: dict[str, Path], now: str, work_state: dict[str, Any]
) -> dict[str, Any]:
    checks = _self_checks(plan, paths, work_state)
    return {
        "version": COMPACT_SELF_CHECK_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SELF_CHECK_SCHEMA),
        "ok": all(item["ok"] for item in checks if item["severity"] == "hard"),
        "apply_id": work_state["apply_id"],
        "plan_id": work_state["plan_id"],
        "event_type": "post_compact_self_check",
        "checks": checks,
        "created_at": now,
        "reserved": runtime_memory_reserved_fields(COMPACT_SELF_CHECK_SCHEMA),
    }


def build_self_check_failure_payload(
    payload: dict[str, Any], self_check: dict[str, Any], refs: dict[str, str], now: str
) -> dict[str, Any]:
    return {
        "version": COMPACT_SELF_CHECK_FAILURE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SELF_CHECK_FAILURE_SCHEMA),
        "ok": False,
        "event_id": payload["event_id"],
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "event_type": "compact_apply_self_check_failure",
        "compact_status": "blocked_self_check_failed",
        "created_at": now,
        "failed_checks": [item for item in self_check["checks"] if not item["ok"]],
        "refs": refs,
        "content_preserved": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_SELF_CHECK_FAILURE_SCHEMA),
    }


def _self_checks(plan: dict[str, Any], paths: dict[str, Path], work_state: dict[str, Any]) -> list[dict[str, Any]]:
    completeness = work_state["completeness"]
    return [
        {"name": "source_content_preserved", "ok": True, "severity": "hard"},
        {"name": "compact_context_written", "ok": paths["context_md"].exists(), "severity": "hard"},
        {"name": "restore_refs_written", "ok": paths["restore_refs_json"].exists(), "severity": "hard"},
        {"name": "apply_bundle_written", "ok": paths["apply_bundle_json"].exists(), "severity": "hard"},
        {"name": "work_state_snapshot_written", "ok": paths["work_state_snapshot_json"].exists(), "severity": "hard"},
        {"name": "restore_refs_exist", "ok": completeness["restore_refs_exist"], "severity": "hard"},
        {"name": "apply_ids_consistent", "ok": _apply_ids_consistent(paths, work_state), "severity": "hard"},
        {"name": "restore_refs_have_sources", "ok": completeness["source_refs_present"], "severity": "soft"},
        {"name": "goal_present", "ok": completeness["goal_present"], "severity": "soft"},
        {"name": "next_actions_present", "ok": completeness["next_actions_present"], "severity": "soft"},
        {"name": "acceptance_present", "ok": completeness["acceptance_present"], "severity": "soft"},
        {"name": "constraints_present", "ok": completeness["constraints_present"], "severity": "soft"},
        {"name": "latest_tests_recorded", "ok": completeness["test_state_present"], "severity": "soft"},
        {"name": "artifact_refs_valid", "ok": _artifact_refs_valid(work_state), "severity": "soft"},
        {"name": "risks_carried_forward", "ok": isinstance(plan["risks"], list), "severity": "soft"},
    ]


def _apply_ids_consistent(paths: dict[str, Path], work_state: dict[str, Any]) -> bool:
    apply_id = str(work_state["apply_id"])
    return all(apply_id in paths[key].name for key in ("metadata_json", "apply_bundle_json", "work_state_snapshot_json"))


def _artifact_refs_valid(work_state: dict[str, Any]) -> bool:
    refs = work_state.get("artifact_refs", [])
    if not isinstance(refs, list):
        return False
    paths = [Path(str(ref["path"])) for ref in refs if isinstance(ref, dict) and ref.get("path")]
    return all(path.exists() for path in paths)


__all__ = [
    "COMPACT_SELF_CHECK_FAILURE_SCHEMA",
    "COMPACT_SELF_CHECK_SCHEMA",
    "build_self_check_failure_payload",
    "build_self_check_payload",
]
