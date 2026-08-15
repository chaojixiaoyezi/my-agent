from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .home_layout import MyAgentHomePaths
from .home_retention import OwnerRetentionPlan, apply_owner_retention

_DEFAULT_INTERVAL_SECONDS = 86_400


@dataclass(frozen=True)
class OwnerMaintenanceResult:
    ran: bool
    status: str
    retention: OwnerRetentionPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ran": self.ran, "status": self.status}
        if self.retention is not None:
            payload["retention"] = self.retention.to_dict()
        return payload


def owner_maintenance_due(
    owner_home: str | Path,
    *,
    now: float | None = None,
) -> bool:
    home = Path(owner_home)
    policy = read_json_object_report(
        home / "retention.json",
        context="owner_maintenance.policy",
    ).payload
    if not bool(policy.get("maintenance_enabled", True)):
        return False
    interval = _positive_interval(policy.get("maintenance_interval_seconds"))
    if interval <= 0:
        return False
    marker = read_json_object_report(
        _maintenance_state_path(home),
        context="owner_maintenance.state",
    ).payload
    last_attempt = _timestamp(marker.get("last_attempt_at"))
    current = float(now if now is not None else time.time())
    return last_attempt <= 0 or current - last_attempt >= interval


def run_owner_retention_if_due(
    home: MyAgentHomePaths,
    *,
    now: float | None = None,
) -> OwnerMaintenanceResult:
    current = float(now if now is not None else time.time())
    if not owner_maintenance_due(home.owner_home_dir, now=current):
        return OwnerMaintenanceResult(False, "not_due")
    state_path = _maintenance_state_path(home.owner_home_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(state_path):
        if not owner_maintenance_due(home.owner_home_dir, now=current):
            return OwnerMaintenanceResult(False, "not_due")
        retention = apply_owner_retention(
            home,
            now=datetime.fromtimestamp(current, timezone.utc),
        )
        status = _maintenance_status(retention)
        previous = read_json_object_report(
            state_path,
            context="owner_maintenance.state",
        ).payload
        payload = {
            "schema_version": "owner-maintenance.v1",
            "last_attempt_at": current,
            "last_success_at": (
                current
                if status == "success"
                else _timestamp(previous.get("last_success_at"))
            ),
            "status": status,
            "action_count": len(retention.actions),
            "failed_action_count": sum(
                action.status in {"failed", "collision", "state_changed"}
                for action in retention.actions
            ),
            "load_errors": list(retention.load_errors),
            "legal_hold": retention.legal_hold,
        }
        write_json_file_atomic_unlocked(state_path, payload)
        return OwnerMaintenanceResult(True, status, retention)


def _maintenance_status(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    if retention.load_errors:
        return "policy_unavailable"
    if any(action.status in {"failed", "collision"} for action in retention.actions):
        return "partial_failure"
    return "success"


def _maintenance_state_path(owner_home: Path) -> Path:
    return owner_home / "data" / "maintenance.json"


def _positive_interval(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_SECONDS
    return max(0, parsed)


def _timestamp(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "OwnerMaintenanceResult",
    "owner_maintenance_due",
    "run_owner_retention_if_due",
]
