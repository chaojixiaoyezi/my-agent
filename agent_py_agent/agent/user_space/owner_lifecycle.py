from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    JsonObjectReadReport,
    append_private_jsonl_records,
    read_json_object_report,
    write_private_text_file_atomic,
)
from ..common.nofollow_fs import ensure_private_dir
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


# LLM: 状态文件目录缺失按 0700 新建（pdp）；状态 JSON 与审计账都走私有写（0600/0700），内容逐字节不变。
# 函数用途: 更新 owner 生命周期状态并追加一条审计记录。
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
    ensure_private_dir(path.parent)
    # 私有原子写：0600/0700，存量宽权限状态文件下次写入即收紧；序列化内容逐字节不变。
    write_private_text_file_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    # 私有追加：0600/0700，存量宽权限 owner 审计账下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(
        owner.home_dir / "audit_log.jsonl",
        [{
            "schema_version": "owner-audit-event.v1",
            "event_type": "owner_lifecycle_updated",
            "owner_id": owner.owner_id,
            "from_status": previous.status,
            "to_status": payload["status"],
            "reason": payload["reason"],
            "updated_at": payload["updated_at"],
        }],
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
