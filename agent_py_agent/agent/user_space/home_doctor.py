# LLM: Home doctor reports owner-home health and cleanup advice; it does not gate normal tasks.
# 模块用途: 汇总 owner home 迁移、索引、retention 和 schema 状态，供 CLI/doctor/人工排查使用。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .home_indexes import dangling_index_refs
from .home_layout import MyAgentHomePaths
from .home_migration import plan_home_migration
from .home_retention import plan_owner_retention
from .home_runtime_query import home_runtime_status


def build_home_doctor_report(home: MyAgentHomePaths) -> dict[str, Any]:
    migration = plan_home_migration(home)
    dangling = dangling_index_refs(home)
    retention = plan_owner_retention(home)
    schema = _schema_payload(home.system_schema_version_json)
    findings = [
        *_migration_findings(migration.actions),
        *_dangling_findings(dangling),
        *_retention_findings(retention.actions),
        *_schema_findings(schema),
    ]
    return {
        "ok": True,
        "home": home_runtime_status(home),
        "schema": schema,
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


__all__ = ["build_home_doctor_report"]
