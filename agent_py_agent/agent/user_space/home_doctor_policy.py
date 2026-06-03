from __future__ import annotations

from typing import Any

from .home_layout import MyAgentHomePaths
from .owner_policy import read_owner_policy_bundle_report


def owner_policy_doctor_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = read_owner_policy_bundle_report(home)
    return {
        "ok": not report.load_errors,
        "load_errors": list(report.load_errors),
    }


def owner_policy_doctor_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
