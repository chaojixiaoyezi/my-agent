from __future__ import annotations

from typing import Any

from .capability_requests import list_capability_requests_report
from .home_layout import MyAgentHomePaths


def capability_requests_doctor_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = list_capability_requests_report(home)
    rows = report.requests
    open_rows = [row for row in rows if row.status.lower() == "open"]
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


def capability_request_doctor_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
