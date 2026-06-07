
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.json_io import read_json_object_report
from .home_layout import MyAgentHomePaths

CAPABILITY_REQUEST_STATUSES = frozenset({"open", "approved", "denied", "expired", "cancelled", "closed"})


@dataclass(frozen=True)
class OwnerCapabilityRequest:
    request_id: str
    path: Path
    status: str
    capability: str
    requested_by: str
    task_id: str = ""
    reason: str = ""
    note: str = ""
    expires_at: str = ""


@dataclass(frozen=True)
class CreateCapabilityRequest:
    requested_by: str
    capability: str
    reason: str
    task_id: str = ""
    expires_at: str = ""


@dataclass(frozen=True)
class CapabilityRequestsReport:
    requests: list[OwnerCapabilityRequest]
    load_errors: list[dict[str, object]]


def create_capability_request(home: MyAgentHomePaths, request: CreateCapabilityRequest) -> OwnerCapabilityRequest:
    request_id = f"capreq_{uuid4().hex[:12]}"
    path = home.owner_capability_requests_dir / f"{request_id}.json"
    payload = {
        "schema_version": "owner-capability-request.v1",
        "request_id": request_id,
        "status": "open",
        "capability": str(request.capability or "").strip(),
        "requested_by": str(request.requested_by or "").strip(),
        "task_id": str(request.task_id or "").strip(),
        "reason": str(request.reason or "").strip(),
        "expires_at": str(request.expires_at or "").strip(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _request_from_payload(path, payload)


def close_capability_request(home: MyAgentHomePaths, request_id: str, *, status: str, note: str = "") -> OwnerCapabilityRequest:
    path = home.owner_capability_requests_dir / f"{_safe_id(request_id)}.json"
    payload = _read_payload(path)
    payload["status"] = _validated_status(status)
    payload["note"] = str(note or "")
    payload["updated_at"] = _now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _request_from_payload(path, payload)


def list_capability_requests(home: MyAgentHomePaths, *, status: str = "") -> list[OwnerCapabilityRequest]:
    return list_capability_requests_report(home, status=status).requests


def list_capability_requests_report(home: MyAgentHomePaths, *, status: str = "") -> CapabilityRequestsReport:
    wanted = str(status or "").strip()
    rows: list[OwnerCapabilityRequest] = []
    load_errors: list[dict[str, object]] = []
    if not home.owner_capability_requests_dir.exists():
        return CapabilityRequestsReport(rows, load_errors)
    for path in sorted(home.owner_capability_requests_dir.glob("*.json")):
        payload, load_error = _read_payload_report(path)
        if load_error is not None:
            load_errors.append(load_error)
            continue
        item = _request_from_payload(path, payload)
        if not wanted or item.status == wanted:
            rows.append(item)
    return CapabilityRequestsReport(rows, load_errors)


def expire_capability_requests(home: MyAgentHomePaths, *, now: str | None = None) -> list[OwnerCapabilityRequest]:
    current = _parse_time(now or _now_iso())
    expired: list[OwnerCapabilityRequest] = []
    if not home.owner_capability_requests_dir.exists():
        return expired
    for path in sorted(home.owner_capability_requests_dir.glob("*.json")):
        payload, load_error = _read_payload_report(path)
        if load_error is not None:
            continue
        if str(payload.get("status") or "") != "open":
            continue
        expires = _parse_time(str(payload.get("expires_at") or ""))
        if expires is None or expires > current:
            continue
        payload["status"] = "expired"
        payload["updated_at"] = _now_iso()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        expired.append(_request_from_payload(path, payload))
    return expired


def _request_from_payload(path: Path, payload: dict[str, Any]) -> OwnerCapabilityRequest:
    return OwnerCapabilityRequest(
        request_id=str(payload.get("request_id") or path.stem),
        path=path,
        status=str(payload.get("status") or "open"),
        capability=str(payload.get("capability") or ""),
        requested_by=str(payload.get("requested_by") or ""),
        task_id=str(payload.get("task_id") or ""),
        reason=str(payload.get("reason") or ""),
        note=str(payload.get("note") or ""),
        expires_at=str(payload.get("expires_at") or ""),
    )


def _read_payload(path: Path) -> dict[str, Any]:
    return _read_payload_report(path)[0]


def _read_payload_report(path: Path) -> tuple[dict[str, Any], dict[str, object] | None]:
    report = read_json_object_report(path, context="owner_capability_request.read")
    return report.payload, report.load_error


def _safe_id(value: object) -> str:
    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-"} else "-" for char in text)
    return result.strip("-_") or "unknown"


def _validated_status(value: object) -> str:
    text = str(value or "").strip()
    if text in CAPABILITY_REQUEST_STATUSES:
        return text
    raise ValueError("capability_request_status_invalid")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "CreateCapabilityRequest",
    "CapabilityRequestsReport",
    "CAPABILITY_REQUEST_STATUSES",
    "OwnerCapabilityRequest",
    "close_capability_request",
    "create_capability_request",
    "expire_capability_requests",
    "list_capability_requests",
    "list_capability_requests_report",
]
