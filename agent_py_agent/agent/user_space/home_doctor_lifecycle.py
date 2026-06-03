from __future__ import annotations

from typing import Any


def owner_lifecycle_doctor_findings(home_status: dict[str, Any]) -> list[dict[str, Any]]:
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
