# LLM: Home doctor reports owner-home health and cleanup advice; it does not gate normal tasks.
# 模块用途: 汇总 owner home 迁移、索引、retention 和 schema 状态，供 CLI/doctor/人工排查使用。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .capability_requests import list_capability_requests
from .home_backup import latest_home_backup_snapshots
from .home_indexes import dangling_index_refs
from .home_layout import MyAgentHomePaths
from .home_migration import plan_home_migration
from .home_retention import plan_owner_retention
from .home_runtime_query import home_runtime_status
from .temporary_grants import list_temporary_grants


# LLM: build_home_doctor_report summarizes owner-home health without enforcing gates.
# 函数用途: 汇总迁移、索引、retention、备份和 owner 请求状态。
def build_home_doctor_report(home: MyAgentHomePaths) -> dict[str, Any]:
    migration = plan_home_migration(home)
    dangling = dangling_index_refs(home)
    retention = plan_owner_retention(home)
    schema = _schema_payload(home.system_schema_version_json)
    backup = _backup_payload(home)
    capability_requests = _capability_requests_payload(home)
    temporary_grants = _temporary_grants_payload(home)
    findings = [
        *_migration_findings(migration.actions),
        *_dangling_findings(dangling),
        *_retention_findings(retention.actions),
        *_schema_findings(schema),
        *_backup_findings(backup),
        *_capability_request_findings(capability_requests),
        *_temporary_grant_findings(temporary_grants),
    ]
    return {
        "ok": True,
        "home": home_runtime_status(home),
        "schema": schema,
        "backup": backup,
        "capability_requests": capability_requests,
        "temporary_grants": temporary_grants,
        "migration": {
            "pending_count": len(migration.actions),
            "actions": [action.to_dict() for action in migration.actions],
        },
        "indexes": {
            "dangling_count": len(dangling),
            "dangling_refs": dangling,
        },
        "retention": {
            "planned_count": len(retention.actions),
            "actions": [action.to_dict() for action in retention.actions],
        },
        "findings": findings,
    }


# LLM: _schema_payload reads schema version metadata for doctor output.
# 函数用途: 容错读取 system/schema_version.json。
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


# LLM: _backup_payload reports recent snapshot backups without opening copied files.
# 函数用途: 生成 home doctor 的 backup 摘要。
def _backup_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    snapshots = latest_home_backup_snapshots(home, limit=5)
    return {
        "snapshot_count": len(snapshots),
        "latest": snapshots[0] if snapshots else {},
        "snapshots": snapshots,
    }


# LLM: _capability_requests_payload exposes open requests for maintenance visibility.
# 函数用途: 生成能力申请数量和打开状态摘要。
def _capability_requests_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    rows = list_capability_requests(home)
    open_rows = [row for row in rows if row.status.lower() == "open"]
    return {
        "total_count": len(rows),
        "open_count": len(open_rows),
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


# LLM: _temporary_grants_payload exposes active grants for maintenance visibility.
# 函数用途: 生成临时授权数量和激活状态摘要。
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


# LLM: _migration_findings turns migration actions into non-blocking findings.
# 函数用途: 生成 migration_pending 提示。
def _migration_findings(actions: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [
        {
            "kind": "migration_pending",
            "severity": "info",
            "message": "legacy home data can be copied into owner home",
            "count": len(actions),
        }
    ]


# LLM: _dangling_findings reports index refs that point to missing paths.
# 函数用途: 将悬空索引转换成 doctor finding。
def _dangling_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "dangling_index",
            "severity": "warning",
            "message": "global index points to a missing path",
            "missing_path": str(record.get("missing_path") or ""),
        }
        for record in records
    ]


# LLM: _retention_findings keeps cleanup suggestions visible but non-destructive.
# 函数用途: 生成 retention_candidate 提示。
def _retention_findings(actions: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [
        {
            "kind": "retention_candidate",
            "severity": "info",
            "message": "retention policy has expired files to clean",
            "count": len(actions),
        }
    ]


# LLM: _schema_findings reports unreadable schema metadata as a warning.
# 函数用途: 生成 schema_version_unreadable 提示。
def _schema_findings(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if schema.get("ok"):
        return []
    return [
        {
            "kind": "schema_version_unreadable",
            "severity": "warning",
            "message": str(schema.get("error") or "schema_version missing"),
            "path": str(schema.get("path") or ""),
        }
    ]


# LLM: _backup_findings warns when no snapshot exists yet.
# 函数用途: 生成 backup_snapshot_missing 提示。
def _backup_findings(backup: dict[str, Any]) -> list[dict[str, Any]]:
    if int(backup.get("snapshot_count") or 0) > 0:
        return []
    return [
        {
            "kind": "backup_snapshot_missing",
            "severity": "info",
            "message": "no owner-home snapshot backup has been recorded yet",
        }
    ]


# LLM: _capability_request_findings reports open requests without approving them.
# 函数用途: 生成 capability_request_open 提示。
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
        }
    ]


# LLM: _temporary_grant_findings reports active grants without revoking them.
# 函数用途: 生成 temporary_grant_active 提示。
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
        }
    ]


__all__ = ["build_home_doctor_report"]
