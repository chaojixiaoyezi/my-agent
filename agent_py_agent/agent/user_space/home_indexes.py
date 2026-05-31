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


@dataclass(frozen=True)
class RunIndexRef:
    owner_id: str
    run_id: str
    task_id: str
    run_path: str | Path
    status: str


@dataclass(frozen=True)
class AgentIndexRef:
    owner_id: str
    agent_id: str
    task_id: str
    run_path: str | Path
    status: str


@dataclass(frozen=True)
class _IndexSpec:
    kind: str
    path: Path
    path_field: str
    key_fields: tuple[str, ...]


@dataclass(frozen=True)
class _ScopeFilter:
    owner_id: str = ""
    task_id: str = ""


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


def register_run_ref(home: MyAgentHomePaths, ref: RunIndexRef) -> dict[str, Any]:
    payload = {
        "schema_version": "global-run-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "run_id": str(ref.run_id or ""),
        "task_id": str(ref.task_id or ""),
        "run_path": str(ref.run_path),
        "status": str(ref.status or ""),
        "updated_at": _now_iso(),
    }
    append_jsonl(home.global_index_active_runs_jsonl, payload, sort_keys=True)
    return payload


def register_agent_ref(home: MyAgentHomePaths, ref: AgentIndexRef) -> dict[str, Any]:
    payload = {
        "schema_version": "global-agent-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "agent_id": str(ref.agent_id or ""),
        "task_id": str(ref.task_id or ""),
        "run_path": str(ref.run_path),
        "status": str(ref.status or ""),
        "updated_at": _now_iso(),
    }
    append_jsonl(home.global_index_active_agents_jsonl, payload, sort_keys=True)
    return payload


def latest_owner_refs(home: MyAgentHomePaths, *, limit: int = 20) -> list[dict[str, Any]]:
    return _latest_unique_refs(home.global_index_owners_jsonl, key_fields=("owner_id",), limit=limit)


def latest_task_refs(home: MyAgentHomePaths, *, owner_id: str = "", status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    owner = str(owner_id or "")
    wanted_status = str(status or "")
    rows = []
    for record in _latest_unique_refs(home.global_index_active_tasks_jsonl, key_fields=("owner_id", "task_id"), limit=0):
        if owner and str(record.get("owner_id") or "") != owner:
            continue
        if wanted_status and str(record.get("status") or "") != wanted_status:
            continue
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return rows


def latest_run_refs(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return _latest_scoped_refs(
        home.global_index_active_runs_jsonl,
        filters=_ScopeFilter(owner_id=owner_id, task_id=task_id),
        key_fields=("owner_id", "run_id"),
        limit=limit,
    )


def latest_agent_refs(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return _latest_scoped_refs(
        home.global_index_active_agents_jsonl,
        filters=_ScopeFilter(owner_id=owner_id, task_id=task_id),
        key_fields=("owner_id", "agent_id"),
        limit=limit,
    )


def dangling_index_refs(home: MyAgentHomePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    specs = (
        _IndexSpec("task", home.global_index_active_tasks_jsonl, "task_path", ("owner_id", "task_id")),
        _IndexSpec("run", home.global_index_active_runs_jsonl, "run_path", ("owner_id", "run_id")),
        _IndexSpec("agent", home.global_index_active_agents_jsonl, "run_path", ("owner_id", "agent_id")),
    )
    for spec in specs:
        findings.extend(_dangling_refs_for(spec, remaining=limit - len(findings)))
        if len(findings) >= limit:
            return findings[:limit]
    return findings


def _dangling_refs_for(
    spec: _IndexSpec,
    *,
    remaining: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if remaining <= 0:
        return findings
    for record in _latest_unique_refs(spec.path, key_fields=spec.key_fields, limit=0):
        target = Path(str(record.get(spec.path_field) or ""))
        if target.exists():
            continue
        findings.append({"kind": spec.kind, "missing_path": str(target), "record": record})
        if len(findings) >= remaining:
            break
    return findings


def _latest_scoped_refs(
    path: Path,
    *,
    filters: _ScopeFilter,
    key_fields: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    owner = str(filters.owner_id or "")
    task = str(filters.task_id or "")
    rows = []
    for record in _latest_unique_refs(path, key_fields=key_fields, limit=0):
        if owner and str(record.get("owner_id") or "") != owner:
            continue
        if task and str(record.get("task_id") or "") != task:
            continue
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return rows


# LLM: _latest_unique_refs treats append-only indexes as history and returns one current row per identity.
# 函数用途: 从索引尾部倒读并按 owner/task/run/agent key 去重，避免旧路径继续影响恢复和 doctor。
def _latest_unique_refs(path: Path, *, key_fields: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for record in _latest_jsonl_records(path, limit=0):
        key = tuple(str(record.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
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


__all__ = [
    "AgentIndexRef",
    "RunIndexRef",
    "TaskIndexRef",
    "dangling_index_refs",
    "latest_agent_refs",
    "latest_owner_refs",
    "latest_run_refs",
    "latest_task_refs",
    "register_agent_ref",
    "register_owner_ref",
    "register_run_ref",
    "register_task_ref",
]
