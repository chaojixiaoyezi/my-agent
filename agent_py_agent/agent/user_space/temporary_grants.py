
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.json_io import read_json_object_report
from .home_layout import MyAgentHomePaths


@dataclass(frozen=True)
class OwnerTemporaryGrant:
    grant_id: str
    path: Path
    status: str
    granted_to: str
    capability: str
    path_prefix: str
    expires_at: str
    reason: str = ""


@dataclass(frozen=True)
class CreateTemporaryGrant:
    granted_to: str
    capability: str
    path_prefix: str
    expires_at: str
    reason: str = ""


@dataclass(frozen=True)
class TemporaryGrantsReport:
    grants: list[OwnerTemporaryGrant]
    load_errors: list[dict[str, object]]


def create_temporary_grant(home: MyAgentHomePaths, request: CreateTemporaryGrant) -> OwnerTemporaryGrant:
    grant_id = f"grant_{uuid4().hex[:12]}"
    path = home.owner_temporary_grants_dir / f"{grant_id}.json"
    payload = {
        "schema_version": "owner-temporary-grant.v1",
        "grant_id": grant_id,
        "status": "active",
        "granted_to": str(request.granted_to or "").strip(),
        "capability": str(request.capability or "").strip(),
        "path_prefix": str(request.path_prefix or "").strip(),
        "expires_at": str(request.expires_at or "").strip(),
        "reason": str(request.reason or "").strip(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_payload(path, payload)
    return _grant_from_payload(path, payload)


def list_temporary_grants(home: MyAgentHomePaths, *, status: str = "") -> list[OwnerTemporaryGrant]:
    return list_temporary_grants_report(home, status=status).grants


def list_temporary_grants_report(home: MyAgentHomePaths, *, status: str = "") -> TemporaryGrantsReport:
    wanted = str(status or "").strip()
    grants: list[OwnerTemporaryGrant] = []
    load_errors: list[dict[str, object]] = []
    for path in sorted(home.owner_temporary_grants_dir.glob("*.json")):
        report = _read_payload_report(path)
        if report.load_error is not None:
            load_errors.append(report.load_error)
            continue
        row = _grant_from_payload(path, report.payload)
        if not wanted or row.status == wanted:
            grants.append(row)
    return TemporaryGrantsReport(grants, load_errors)


def expire_temporary_grants(home: MyAgentHomePaths, *, now: str | None = None) -> list[OwnerTemporaryGrant]:
    current = _parse_time(now or _now_iso())
    expired: list[OwnerTemporaryGrant] = []
    for path in sorted(home.owner_temporary_grants_dir.glob("*.json")):
        report = _read_payload_report(path)
        if report.load_error is not None:
            continue
        payload = report.payload
        if str(payload.get("status") or "") != "active":
            continue
        expires = _parse_time(str(payload.get("expires_at") or ""))
        if expires is None or expires > current:
            continue
        payload["status"] = "expired"
        payload["updated_at"] = _now_iso()
        _write_payload(path, payload)
        expired.append(_grant_from_payload(path, payload))
    return expired


def _grant_from_payload(path: Path, payload: dict[str, Any]) -> OwnerTemporaryGrant:
    return OwnerTemporaryGrant(
        grant_id=str(payload.get("grant_id") or path.stem),
        path=path,
        status=str(payload.get("status") or "active"),
        granted_to=str(payload.get("granted_to") or ""),
        capability=str(payload.get("capability") or ""),
        path_prefix=str(payload.get("path_prefix") or ""),
        expires_at=str(payload.get("expires_at") or ""),
        reason=str(payload.get("reason") or ""),
    )


def _read_payload_report(path: Path):
    return read_json_object_report(path, context="temporary_grants.read")


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CreateTemporaryGrant",
    "OwnerTemporaryGrant",
    "TemporaryGrantsReport",
    "create_temporary_grant",
    "expire_temporary_grants",
    "list_temporary_grants",
    "list_temporary_grants_report",
]
