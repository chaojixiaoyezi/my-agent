
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .home_backup import latest_home_backup_snapshots_report
from .home_doctor_capability_requests import capability_requests_doctor_payload
from .home_doctor_findings import build_doctor_findings, repair_plan
from .home_doctor_policy import owner_policy_doctor_payload
from .home_indexes import dangling_index_refs
from .home_layout import MyAgentHomePaths
from .home_retention import plan_owner_retention
from .home_runtime_query import home_runtime_status
from .owner_compact_indexes import dangling_owner_compact_index_refs_report
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
    return owner_policy_doctor_payload(home)


def _capability_requests_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    return capability_requests_doctor_payload(home)


def _temporary_grants_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    rows = list_temporary_grants(home)
    active_rows = [row for row in rows if row.status.lower() == "active"]
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
