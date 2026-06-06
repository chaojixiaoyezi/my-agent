
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report
from .home_daily_memory_query import (
    DailyMemoryQuery,
    DailyMemoryRecordsReport,
    daily_memory_files,
    read_daily_memory_records,
    read_daily_memory_records_report,
)
from .home_layout import MyAgentHomePaths, home_paths, safe_task_slug
from .home_runtime_compact_refs import compact_read_paths, task_compact_payload
from .home_runtime_status import home_runtime_status_payload


@dataclass(frozen=True)
class TaskWorkspaceQuery:
    query: str = ""
    date_key: str | None = None
    limit: int = 20


def list_task_workspaces(paths: MyAgentHomePaths | str | Path, request: TaskWorkspaceQuery) -> list[dict[str, Any]]:
    home = _coerce_home_paths(paths)
    items: list[dict[str, Any]] = []
    for state_path in _task_state_files(home, request.date_key):
        item = _task_workspace_payload(state_path)
        if not _matches_query(item, request.query):
            continue
        items.append(item)
        if _limit_reached(items, request.limit):
            break
    return _apply_limit(items, request.limit)


def find_task_workspace(paths: MyAgentHomePaths | str | Path, task_ref: str) -> dict[str, Any] | None:
    ref = str(task_ref or "").strip()
    if not ref:
        return None
    home = _coerce_home_paths(paths)
    for item in list_task_workspaces(home, TaskWorkspaceQuery(limit=0)):
        if _task_workspace_ref_matches(item, ref):
            return item
    return None


def home_task_workspace_payload(paths: MyAgentHomePaths | str | Path, task_ref: str) -> dict[str, Any] | None:
    item = find_task_workspace(paths, task_ref)
    if item is None:
        return None
    state = item["state"]
    reads = _recommended_task_reads(item)
    return {
        "run_id": str(state.get("run_id") or task_ref),
        "task_id": str(state.get("task_id") or state.get("task_name") or task_ref),
        "exists": True,
        "source": "home_task_workspace",
        "status": str(state.get("status") or state.get("source") or "workspace"),
        "verification_status": str(state.get("verification_status") or ""),
        "goal": str(state.get("task_name") or state.get("task_id") or task_ref),
        "updated_at": str(state.get("updated_at") or ""),
        "task_dir": item["root"],
        "state_path": item["state_path"],
        "timeline_path": item["timeline_path"],
        "compact": item.get("compact", {}),
        "recommended_read_paths": reads,
        "authority_validation": _validate_paths(reads),
    }


def home_runtime_status(paths: MyAgentHomePaths | str | Path) -> dict[str, Any]:
    home = _coerce_home_paths(paths)
    return home_runtime_status_payload(
        home,
        daily_files=len(daily_memory_files(home, None)),
        task_workspaces=len(_task_state_files(home, None)),
    )


def _coerce_home_paths(paths: MyAgentHomePaths | str | Path) -> MyAgentHomePaths:
    if isinstance(paths, MyAgentHomePaths):
        return paths
    return home_paths(paths)


def _task_state_files(paths: MyAgentHomePaths, date_key: str | None) -> list[Path]:
    files: list[Path] = []
    for root in _task_workspace_roots(paths):
        files.extend(_task_state_files_under(root, date_key))
    return sorted(dict.fromkeys(files), reverse=True)


def _task_workspace_roots(paths: MyAgentHomePaths) -> tuple[Path, ...]:
    owner_tasks = getattr(paths, "owner_tasks_dir", None)
    roots = [Path(owner_tasks)] if owner_tasks else []
    if _is_local_main_owner(paths):
        roots.append(paths.root / "tasks")
        roots.append(paths.workspace_tasks_dir)
    return tuple(dict.fromkeys(roots))


def _is_local_main_owner(paths: MyAgentHomePaths) -> bool:
    return (
        str(getattr(paths, "owner_provider", "") or "local") == "local"
        and str(getattr(paths, "owner_kind", "") or "main") == "main"
        and str(getattr(paths, "owner_id", "") or "local/main") == "local/main"
    )


def _task_state_files_under(root: Path, date_key: str | None) -> list[Path]:
    if not root.exists():
        return []
    date_dirs = [root / date_key] if date_key else sorted((path for path in root.iterdir() if path.is_dir()), reverse=True)
    state_files: list[Path] = []
    for date_dir in date_dirs:
        if not date_dir.exists():
            continue
        state_files.extend(sorted(date_dir.glob("*/work/state.json"), reverse=True))
        state_files.extend(sorted(date_dir.glob("*/state.json"), reverse=True))
    return state_files


def _task_workspace_payload(state_path: Path) -> dict[str, Any]:
    root = state_path.parent.parent if state_path.parent.name == "work" else state_path.parent
    work = root / "work"
    state_report = read_json_object_report(state_path, context="home_runtime_query.task_state")
    workspace_path = work / "run_workspace.json"
    workspace_report = read_json_object_report(workspace_path, context="home_runtime_query.run_workspace")
    timeline = work / "timeline.jsonl" if (work / "timeline.jsonl").exists() else root / "timeline.jsonl"
    compact_root = work / "compact" if (work / "compact").exists() else root / "compact"
    state_payload = (
        state_report.payload
        if state_report.load_error
        else _state_with_workspace_identity(state_report.payload, workspace_report.payload)
    )
    return {
        "root": str(root),
        "date": root.parent.name,
        "slug": root.name,
        "exists": root.exists(),
        "state_path": str(state_path),
        "workspace_path": str(workspace_path),
        "timeline_path": str(timeline),
        "task_yaml_path": str(work / "task.yaml" if (work / "task.yaml").exists() else root / "task.yaml"),
        "output_dir": str(root / "output"),
        "work_dir": str(work),
        "runtime_dir": str(work / "runtime"),
        "agents_dir": str(work / "agents"),
        "logs_dir": str(work / "logs"),
        "compact_dir": str(compact_root),
        "compact": task_compact_payload(compact_root),
        "state": state_payload,
        "workspace": workspace_report.payload,
        "state_load_error": state_report.load_error or {},
        "workspace_load_error": workspace_report.load_error or {},
    }


def _state_with_workspace_identity(state: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state) if isinstance(state, dict) else {}
    if not isinstance(workspace, dict):
        return payload
    for key in ("request_id", "run_id", "task_id", "task_title", "prompt_fingerprint", "owner_id", "owner_home", "task_name", "source"):
        if not payload.get(key) and workspace.get(key):
            payload[key] = workspace[key]
    return payload


def _task_workspace_ref_matches(item: dict[str, Any], ref: str) -> bool:
    state = item.get("state", {}) if isinstance(item.get("state"), dict) else {}
    workspace = item.get("workspace", {}) if isinstance(item.get("workspace"), dict) else {}
    candidates = [
        item.get("slug", ""),
        safe_task_slug(ref),
        state.get("task_id", ""),
        state.get("task_name", ""),
        state.get("run_id", ""),
        state.get("request_id", ""),
        workspace.get("task_id", ""),
        workspace.get("task_name", ""),
        workspace.get("run_id", ""),
        workspace.get("request_id", ""),
    ]
    return ref in {str(value) for value in candidates} or safe_task_slug(ref) == str(item.get("slug") or "")


def _recommended_task_reads(item: dict[str, Any]) -> list[str]:
    paths = [
        item.get("workspace_path", ""),
        item.get("state_path", ""),
        item.get("timeline_path", ""),
        item.get("task_yaml_path", ""),
        *compact_read_paths(item),
    ]
    return [str(path) for path in paths if path and Path(str(path)).exists()]


def _validate_paths(paths: list[str]) -> dict[str, Any]:
    missing = [path for path in paths if path and not Path(path).exists()]
    return {"ok": not missing, "missing_paths": missing}


def _matches_query(item: dict[str, Any], query: str) -> bool:
    return _text_contains(item, query)


def _text_contains(value: dict[str, Any], query: str) -> bool:
    text = str(query or "").strip().lower()
    if not text:
        return True
    return text in json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _limit_reached(items: list[dict[str, Any]], limit: int) -> bool:
    return int(limit or 0) > 0 and len(items) >= int(limit)


def _apply_limit(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    value = int(limit or 0)
    return items[:value] if value > 0 else items


__all__ = [
    "DailyMemoryQuery",
    "DailyMemoryRecordsReport",
    "TaskWorkspaceQuery",
    "find_task_workspace",
    "home_runtime_status",
    "home_task_workspace_payload",
    "list_task_workspaces",
    "read_daily_memory_records",
    "read_daily_memory_records_report",
]
