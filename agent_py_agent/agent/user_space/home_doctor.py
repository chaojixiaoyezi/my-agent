
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capability_requests import list_capability_requests_report
from .home_backup import latest_home_backup_snapshots_report
from .home_indexes import dangling_index_refs
from .home_layout import MyAgentHomePaths
from .home_retention import plan_owner_retention
from .home_runtime_query import home_runtime_status
from .owner_compact_indexes import dangling_owner_compact_index_refs_report
from .owner_policy import read_owner_policy_bundle_report
from .temporary_grants import list_temporary_grants


def build_home_doctor_report(home: MyAgentHomePaths) -> dict[str, Any]:
    sections = _collect_doctor_sections(home)
    findings = build_doctor_findings(sections)
    return _doctor_report(home, sections, findings)


def _collect_doctor_sections(home: MyAgentHomePaths) -> dict[str, Any]:
    home_status = home_runtime_status(home)
    dangling = dangling_index_refs(home)
    compact_index_report = dangling_owner_compact_index_refs_report(home.owner_home_dir)
    retention = plan_owner_retention(home)
    schema = _schema_payload(home.system_schema_version_json)
    backup = _backup_payload(home)
    owner_policy = _owner_policy_payload(home)
    capability_requests = _capability_requests_payload(home)
    temporary_grants = _temporary_grants_payload(home)
    return {
        "home": home_status,
        "dangling": dangling,
        "compact_dangling": compact_index_report.dangling_refs,
        "compact_index_load_errors": compact_index_report.load_errors,
        "retention": retention,
        "schema": schema,
        "backup": backup,
        "owner_policy": owner_policy,
        "capability_requests": capability_requests,
        "temporary_grants": temporary_grants,
    }


def _doctor_report(home: MyAgentHomePaths, sections: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "home": sections["home"],
        "schema": sections["schema"],
        "backup": sections["backup"],
        "owner_policy": sections["owner_policy"],
        "capability_requests": sections["capability_requests"],
        "temporary_grants": sections["temporary_grants"],
        "indexes": {
            "dangling_count": len(sections["dangling"]),
            "dangling_refs": sections["dangling"],
        },
        "compact_indexes": {
            "dangling_count": len(sections["compact_dangling"]),
            "dangling_refs": sections["compact_dangling"],
            "load_errors": sections["compact_index_load_errors"],
        },
        "retention": {
            "planned_count": len(sections["retention"].actions),
            "actions": [action.to_dict() for action in sections["retention"].actions],
        },
        "repair_plan": repair_plan(findings),
        "findings": findings,
    }


def build_doctor_findings(sections: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *_owner_lifecycle_findings(sections["home"]),
        *_dangling_findings(sections["dangling"]),
        *_compact_index_findings(sections["compact_dangling"]),
        *_compact_index_load_error_findings(sections["compact_index_load_errors"]),
        *_retention_findings(sections["retention"].actions),
        *_schema_findings(sections["schema"]),
        *_backup_findings(sections["backup"]),
        *_owner_policy_findings(sections["owner_policy"]),
        *_capability_request_findings(sections["capability_requests"]),
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


def _owner_lifecycle_findings(home_status: dict[str, Any]) -> list[dict[str, Any]]:
    lifecycle = home_status.get("owner", {}).get("lifecycle", {})
    load_error = lifecycle.get("load_error") if isinstance(lifecycle, dict) else None
    if not isinstance(load_error, dict):
        return []
    return [
        {
            "kind": "owner_lifecycle_load_error",
            "path": load_error.get("path") or lifecycle.get("path", ""),
            "status": "UNKNOWN",
            "note": "owner lifecycle status is unreadable; do not treat this owner as confirmed active",
            "load_error": load_error,
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
                note="inspect schema_version.json or rerun home bootstrap before trusting schema status",
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
                note="create a snapshot before destructive maintenance or schema changes",
            ),
        }
    ]


def _owner_policy_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = payload.get("load_errors") if isinstance(payload.get("load_errors"), list) else []
    return [
        {
            "kind": "owner_policy_load_error",
            "severity": "warning",
            "message": "owner policy file could not be read; defaults may be incomplete",
            "context": str(error.get("context") or ""),
            "path": str(error.get("path") or ""),
            "resolution": {
                "action_class": "manual",
                "note": "inspect the owner policy file before trusting permissions, quota, retention, skills or tools",
            },
        }
        for error in errors
        if isinstance(error, dict)
    ]


def _capability_request_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(payload.get("open_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "kind": "capability_request_open",
            "severity": "info",
            "message": "owner has open capability requests",
            "count": count,
            "resolution": {
                "action_class": "manual",
                "note": "review or expire pending capability requests; do not auto-approve them",
            },
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


def _schema_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": str(path),
        "ok": isinstance(payload, dict) and bool(payload.get("schema_version")),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _backup_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = latest_home_backup_snapshots_report(home, limit=5)
    snapshots = report.snapshots
    return {
        "snapshot_count": len(snapshots),
        "latest": snapshots[0] if snapshots else {},
        "snapshots": snapshots,
        "load_errors": report.load_errors,
    }


def _owner_policy_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = read_owner_policy_bundle_report(home)
    return {
        "ok": not report.load_errors,
        "load_errors": list(report.load_errors),
    }


def _capability_requests_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = list_capability_requests_report(home)
    rows = report.requests
    open_rows = [row for row in rows if row.status == "open"]
    return {
        "total_count": len(rows),
        "open_count": len(open_rows),
        "load_errors": report.load_errors,
        "open": [
            {
                "request_id": row.request_id,
                "capability": row.capability,
                "requested_by": row.requested_by,
                "task_id": row.task_id,
                "expires_at": row.expires_at,
            }
            for row in open_rows
        ],
    }


def _temporary_grants_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    rows = list_temporary_grants(home)
    active_rows = [row for row in rows if row.status == "active"]
    return {
        "total_count": len(rows),
        "active_count": len(active_rows),
        "active": [
            {
                "grant_id": row.grant_id,
                "capability": row.capability,
                "granted_to": row.granted_to,
                "path_prefix": row.path_prefix,
                "expires_at": row.expires_at,
            }
            for row in active_rows
        ],
    }


__all__ = ["build_home_doctor_report"]
