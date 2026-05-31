# LLM: Global indexes are rebuildable maps; never treat them as task truth.
# 模块用途: 写 owner/task/run/agent 的轻量引用索引，正文仍以各自 owner home 下文件为准。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .home_layout import MyAgentHomePaths
from .owner_resolver import OwnerHomeResult


@dataclass(frozen=True)
class TaskIndexRef:
    owner_id: str
    task_id: str
    task_path: str | Path
    status: str
    title: str = ""


def register_owner_ref(home: MyAgentHomePaths, owner: OwnerHomeResult) -> dict[str, Any]:
    payload = {
        "schema_version": "global-owner-index.v1",
        "owner_id": owner.owner_id,
        "provider": owner.identity.provider,
        "owner_kind": owner.identity.owner_kind,
        "owner_home": str(owner.home_dir),
        "updated_at": _now_iso(),
    }
    append_jsonl(home.global_index_owners_jsonl, payload, sort_keys=True)
    return payload


def register_task_ref(home: MyAgentHomePaths, ref: TaskIndexRef) -> dict[str, Any]:
    payload = {
        "schema_version": "global-task-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "task_id": str(ref.task_id or ""),
        "task_path": str(ref.task_path),
        "status": str(ref.status or ""),
        "title": str(ref.title or ""),
        "updated_at": _now_iso(),
    }
    append_jsonl(home.global_index_active_tasks_jsonl, payload, sort_keys=True)
    return payload


def latest_owner_refs(home: MyAgentHomePaths, *, limit: int = 20) -> list[dict[str, Any]]:
    return _latest_jsonl_records(home.global_index_owners_jsonl, limit=limit)


def latest_task_refs(home: MyAgentHomePaths, *, owner_id: str = "", status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    owner = str(owner_id or "")
    wanted_status = str(status or "")
    rows = []
    for record in _latest_jsonl_records(home.global_index_active_tasks_jsonl, limit=0):
        if owner and str(record.get("owner_id") or "") != owner:
            continue
        if wanted_status and str(record.get("status") or "") != wanted_status:
            continue
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return rows


def _latest_jsonl_records(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
        if limit > 0 and len(rows) >= limit:
            break
    return rows


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["TaskIndexRef", "latest_owner_refs", "latest_task_refs", "register_owner_ref", "register_task_ref"]
