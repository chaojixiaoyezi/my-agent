from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import JsonObjectReadReport, read_json_object_report
from ..io import append_jsonl
from .owner_resolver import OwnerHomeResult


@dataclass(frozen=True)
class OwnerLifecycleState:
    owner_id: str
    status: str
    path: Path
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "status": self.status,
            "path": str(self.path),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OwnerLifecycleReport:
    state: OwnerLifecycleState
    load_error: dict[str, object] | None = None


def read_owner_lifecycle(owner: OwnerHomeResult) -> OwnerLifecycleState:
    return read_owner_lifecycle_report(owner).state


def read_owner_lifecycle_report(owner: OwnerHomeResult) -> OwnerLifecycleReport:
    path = _lifecycle_path(owner)
    report = _read_json_report(path)
    payload = report.payload
    status = "UNKNOWN" if report.load_error else str(payload.get("status") or "active")
    return OwnerLifecycleReport(
        OwnerLifecycleState(
            owner_id=owner.owner_id,
            status=status,
            reason=str(payload.get("reason") or ""),
            path=path,
        ),
        report.load_error,
    )


def update_owner_lifecycle(owner: OwnerHomeResult, *, status: str, reason: str = "") -> OwnerLifecycleState:
    path = _lifecycle_path(owner)
    previous = read_owner_lifecycle(owner)
    payload = {
        "schema_version": "owner-lifecycle.v1",
        "owner_id": owner.owner_id,
        "provider": owner.identity.provider,
        "owner_kind": owner.identity.owner_kind,
        "status": _safe_status(status),
        "reason": str(reason or ""),
        "previous_status": previous.status,
        "updated_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_jsonl(
        owner.home_dir / "audit_log.jsonl",
        {
            "schema_version": "owner-audit-event.v1",
            "event_type": "owner_lifecycle_updated",
            "owner_id": owner.owner_id,
            "from_status": previous.status,
            "to_status": payload["status"],
            "reason": payload["reason"],
            "updated_at": payload["updated_at"],
        },
        sort_keys=True,
    )
    return read_owner_lifecycle(owner)


def _lifecycle_path(owner: OwnerHomeResult) -> Path:
    return owner.home_dir / "owner_status.json"


def _safe_status(value: object) -> str:
    text = str(value or "active").strip().lower()
    return "".join(char if char.isalnum() or char in {"_", "-"} else "-" for char in text).strip("-_") or "active"


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_report(path).payload


def _read_json_report(path: Path) -> JsonObjectReadReport:
    return read_json_object_report(path, context="owner_lifecycle.status")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["OwnerLifecycleReport", "OwnerLifecycleState", "read_owner_lifecycle", "read_owner_lifecycle_report", "update_owner_lifecycle"]
