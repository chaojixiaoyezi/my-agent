from __future__ import annotations

from typing import Any

from .home_doctor_capability_requests import capability_request_doctor_findings
from .home_doctor_lifecycle import owner_lifecycle_doctor_findings
from .home_doctor_policy import owner_policy_doctor_findings


def build_doctor_findings(sections: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *owner_lifecycle_doctor_findings(sections["home"]),
        *_migration_findings(sections["migration"].actions),
        *_dangling_findings(sections["dangling"]),
        *_compact_index_findings(sections["compact_dangling"]),
        *_compact_index_load_error_findings(sections["compact_index_load_errors"]),
        *_retention_findings(sections["retention"].actions),
        *_schema_findings(sections["schema"]),
        *_backup_findings(sections["backup"]),
        *owner_policy_doctor_findings(sections["owner_policy"]),
        *capability_request_doctor_findings(sections["capability_requests"]),
        *_temporary_grant_findings(sections["temporary_grants"]),
    ]


def repair_plan(findings: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {"auto_repair": [], "warn": [], "manual": []}
    for finding in findings:
        resolution = finding.get("resolution") if isinstance(finding.get("resolution"), dict) else {}
        action_class = str(resolution.get("action_class") or "manual")
        if action_class not in grouped:
            action_class = "manual"
        grouped[action_class].append(
            {
                "kind": str(finding.get("kind") or ""),
                "message": str(finding.get("message") or ""),
                "command": str(resolution.get("command") or ""),
                "note": str(resolution.get("note") or ""),
            }
        )
    return {
        "auto_repair_count": len(grouped["auto_repair"]),
        "warn_count": len(grouped["warn"]),
        "manual_count": len(grouped["manual"]),
        "auto_repair": grouped["auto_repair"],
        "warn": grouped["warn"],
        "manual": grouped["manual"],
    }


def _migration_findings(actions: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [
        {
            "kind": "migration_pending",
            "severity": "info",
            "message": "legacy home data can be copied into owner home",
            "count": len(actions),
            "resolution": _resolution(
                "auto_repair",
                command="my-agent home-migrate --apply",
                note="copy legacy memory and task workspace files into the current owner home",
            ),
        }
    ]


def _dangling_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "dangling_index",
            "severity": "warning",
            "message": "global index points to a missing path",
            "missing_path": str(record.get("missing_path") or ""),
            "resolution": _resolution(
                "auto_repair",
                command="my-agent home-index-rebuild --apply",
                note="rebuild owner/task/run/agent indexes from existing owner homes",
            ),
        }
        for record in records
    ]


def _compact_index_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "dangling_compact_index",
            "severity": "warning",
            "message": "owner compact index points to a missing task rollup or compact package",
            "pointer": str(record.get("pointer") or ""),
            "field": str(record.get("field") or ""),
            "missing_path": str(record.get("missing_path") or ""),
            "resolution": _resolution(
                "manual",
                note="resync the affected task compact rollup after confirming the task workspace still exists",
            ),
        }
        for record in records
    ]


def _compact_index_load_error_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "compact_index_pointer_unreadable",
            "severity": "warning",
            "message": "owner compact index pointer is not valid JSON",
            "path": str(record.get("path") or ""),
            "error": str(record.get("error") or ""),
            "resolution": _resolution(
                "manual",
                note="remove the unreadable pointer or regenerate task compact rollup after confirming the task workspace",
            ),
        }
        for record in records
    ]


def _retention_findings(actions: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [
        {
            "kind": "retention_candidate",
            "severity": "info",
            "message": "retention policy has expired files to clean",
            "count": len(actions),
            "resolution": _resolution(
                "auto_repair",
                command="my-agent home-retention --apply",
                note="delete only files selected by the explicit owner retention policy",
            ),
        }
    ]


def _schema_findings(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if schema.get("ok"):
        return []
    return [
        {
            "kind": "schema_version_unreadable",
            "severity": "warning",
            "message": str(schema.get("error") or "schema_version missing"),
            "path": str(schema.get("path") or ""),
            "resolution": _resolution(
                "manual",
                note="inspect schema_version.json or rerun home bootstrap before trusting migration status",
            ),
        }
    ]


def _backup_findings(backup: dict[str, Any]) -> list[dict[str, Any]]:
    if int(backup.get("snapshot_count") or 0) > 0:
        return []
    return [
        {
            "kind": "backup_snapshot_missing",
            "severity": "info",
            "message": "no owner-home snapshot backup has been recorded yet",
            "resolution": _resolution(
                "warn",
                note="create a snapshot before destructive maintenance or schema migration",
            ),
        }
    ]


def _temporary_grant_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(payload.get("active_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "kind": "temporary_grant_active",
            "severity": "info",
            "message": "owner has active temporary grants",
            "count": count,
            "resolution": _resolution(
                "warn",
                note="verify active grants are still intended; expired grants are handled by retention/doctor lifecycle",
            ),
        }
    ]


def _resolution(action_class: str, *, command: str = "", note: str = "") -> dict[str, str]:
    payload = {"action_class": action_class, "note": note}
    if command:
        payload["command"] = command
    return payload


__all__ = ["build_doctor_findings", "repair_plan"]
