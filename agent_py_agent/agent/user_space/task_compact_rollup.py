
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import JsonObjectReadReport, read_json_object_report, write_json_object
from ..subagents.models import TaskStatus, normalize_task_status
from ..task_progress import read_task_progress_report, task_progress_summary
from .compact_layout import CompactPackageRequest, ensure_compact_package
from .owner_compact_indexes import sync_owner_compact_indexes
from .task_compact_rollup_signature import RollupEventRequest, append_rollup_event_if_changed

_ACTIONABLE_STATUS_BUCKETS = frozenset({"pending", "running", "paused", "blocked", "failed", "timeout", "unknown"})
_BLOCKED_STATUS_BUCKETS = frozenset({"blocked", "failed", "timeout"})
_STATUS_BUCKETS_BY_PROTOCOL = {
    TaskStatus.DONE.value: "done",
    TaskStatus.BLOCKED.value: "blocked",
    TaskStatus.FAILED.value: "failed",
    TaskStatus.CHANNEL_ERROR.value: "failed",
    TaskStatus.TIMEOUT.value: "timeout",
    TaskStatus.RUNNING.value: "running",
    TaskStatus.PENDING.value: "pending",
    TaskStatus.PLANNING.value: "pending",
    TaskStatus.PAUSED.value: "paused",
    TaskStatus.CANCELLED.value: "cancelled",
    TaskStatus.ABANDONED.value: "abandoned",
    TaskStatus.TAKEN_OVER.value: "taken_over",
}


@dataclass(frozen=True)
class TaskCompactRollupResult:
    task_workspace: Path
    compact_root: Path
    rollup_json: Path
    rollup_markdown: Path
    compact_package_dir: Path
    child_count: int


def sync_task_compact_rollup(task_workspace: str | Path, *, compact_index: int | None = None) -> TaskCompactRollupResult:
    task_root = Path(task_workspace)
    work_root = _task_work_root(task_root)
    compact_root = work_root / "compact"
    index = compact_index if compact_index is not None else current_or_first_compact_index(compact_root)
    package = ensure_compact_package(compact_root, CompactPackageRequest(compact_index=index, scope="task"))
    child_runs = _child_run_records(task_root)
    rollup_json = compact_root / "task_rollup.json"
    rollup_md = compact_root / "task_rollup.md"
    rollup = _rollup_payload(task_root, child_runs, package.package_dir, rollup_json=rollup_json)
    write_json_object(rollup_json, rollup)
    _write_branch_rollup(compact_root, rollup, branch_id="main")
    rollup_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    write_json_object(package.work_state_snapshot_json, _work_state_payload(rollup))
    write_json_object(package.refs_json, _refs_payload(rollup_json, rollup_md, child_runs))
    write_json_object(package.continue_packet_json, _continue_packet_payload(rollup))
    package.handoff_summary_md.write_text(_rollup_markdown(rollup), encoding="utf-8")
    append_rollup_event_if_changed(RollupEventRequest(compact_root, task_root, rollup, package.package_dir, child_runs))
    sync_owner_compact_indexes(task_root, rollup)
    return TaskCompactRollupResult(
        task_workspace=task_root,
        compact_root=compact_root,
        rollup_json=rollup_json,
        rollup_markdown=rollup_md,
        compact_package_dir=package.package_dir,
        child_count=len(child_runs),
    )

def _write_branch_rollup(compact_root: Path, rollup: dict[str, object], *, branch_id: str) -> Path:
    branch_payload = {
        **rollup,
        "schema_version": "task-compact-branch-rollup.v1",
        "branch_id": _safe_branch_id(branch_id),
    }
    path = compact_root / "rollups" / f"branch_{_safe_branch_id(branch_id)}_rollup.json"
    write_json_object(path, branch_payload)
    return path


def _child_run_records(task_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    agents_root = _agents_root(task_root)
    for state_path in sorted(agents_root.glob("*/state.json")):
        state_report = _read_json_report(state_path, context="task_compact_rollup.child_state")
        payload = state_report.payload
        run_id = str(payload.get("id") or payload.get("run_id") or state_path.parent.name)
        attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
        system_tree = attrs.get("system_tree") if isinstance(attrs.get("system_tree"), dict) else {}
        row: dict[str, object] = {
            "run_id": run_id,
            "status": str(payload.get("status") or system_tree.get("status") or ""),
            "progress": _safe_float(payload.get("progress") or system_tree.get("progress")),
            "summary": str(payload.get("latest_summary") or system_tree.get("latest_summary") or "")[:500],
            "refs": {
                "state": str(state_path),
                "compact": str(state_path.parent / "compactions"),
                "summary": str(state_path.parent / "summary.md"),
                "final_report": str(state_path.parent / "final_report.md"),
                "artifact_manifest": str(state_path.parent / "artifacts" / "manifest.jsonl"),
            },
            "artifact_refs": _list_strings(payload.get("artifact_refs") or system_tree.get("artifact_refs")),
            "blockers": _list_strings(payload.get("blockers") or system_tree.get("blockers")),
        }
        if state_report.load_error is not None:
            row["status"] = "UNKNOWN"
            row["state_load_error"] = state_report.load_error
        rows.append(row)
    return rows


def _rollup_payload(
    task_root: Path,
    child_runs: list[dict[str, object]],
    package_dir: Path,
    *,
    rollup_json: Path,
) -> dict[str, object]:
    task_state_report = _read_json_report(_task_state_path(task_root), context="task_compact_rollup.task_state")
    state = task_state_report.payload
    status_groups = _status_groups(child_runs)
    progress, progress_load_error = _root_task_progress(task_root, str(state.get("task_id") or ""))
    load_errors = [
        *([task_state_report.load_error] if task_state_report.load_error is not None else []),
        *([progress_load_error] if progress_load_error is not None else []),
        *[
            row["state_load_error"]
            for row in child_runs
            if isinstance(row.get("state_load_error"), dict)
        ],
    ]
    return {
        "schema_version": "task-compact-rollup.v1",
        "task_id": str(state.get("task_id") or task_root.name),
        "task_workspace": str(task_root),
        "status": str(state.get("status") or ""),
        "progress": _safe_float(state.get("progress")),
        "primary_run_id": str(state.get("primary_run_id") or ""),
        "compact_package": str(package_dir),
        "rollup_json": str(rollup_json),
        "child_runs": child_runs,
        "child_count": len(child_runs),
        "status_counts": _status_counts(child_runs),
        "completed_run_ids": status_groups["completed"],
        "pending_run_ids": status_groups["pending"],
        "blocked_run_ids": status_groups["blocked"],
        "artifact_refs": _unique_strings(
            ref
            for row in child_runs
            for ref in _list_strings(row.get("artifact_refs"))
        ),
        "task_progress": progress,
        "load_errors": load_errors,
        "updated_at": _now_iso(),
    }


def _task_work_root(task_root: Path) -> Path:
    return task_root / "work"


def current_or_first_compact_index(compact_root: Path) -> int:
    latest = compact_root / "latest.txt"
    if latest.exists():
        parsed = compact_index_from_name(latest.read_text(encoding="utf-8").strip())
        if parsed:
            return parsed
    latest_link = compact_root / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        try:
            parsed = compact_index_from_name(latest_link.resolve().name)
        except OSError:
            parsed = compact_index_from_name(latest_link.name)
        if parsed:
            return parsed
    existing = sorted(compact_root.glob("compact_[0-9][0-9][0-9][0-9]"))
    if existing:
        return compact_index_from_name(existing[-1].name) or 1
    return 1


def compact_index_from_name(value: str) -> int:
    text = str(value or "").strip()
    if not text.startswith("compact_"):
        return 0
    try:
        return int(text.rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _agents_root(task_root: Path) -> Path:
    return task_root / "work" / "agents"


def _task_state_path(task_root: Path) -> Path:
    return task_root / "work" / "state.json"


def _work_state_payload(rollup: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "work-state-snapshot.v1",
        "scope": "task",
        "task_id": rollup.get("task_id", ""),
        "status": rollup.get("status", ""),
        "progress": rollup.get("progress", 0.0),
        "child_count": rollup.get("child_count", 0),
        "status_counts": rollup.get("status_counts", {}),
        "pending_run_ids": rollup.get("pending_run_ids", []),
        "blocked_run_ids": rollup.get("blocked_run_ids", []),
        "task_progress": rollup.get("task_progress", {}),
        "updated_at": rollup.get("updated_at", ""),
    }


def _refs_payload(rollup_json: Path, rollup_md: Path, child_runs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "compact-refs.v1",
        "refs": [
            {"kind": "task_rollup", "path": str(rollup_json)},
            {"kind": "task_rollup_markdown", "path": str(rollup_md)},
            *[
                {"kind": "child_run_state", "run_id": row.get("run_id", ""), "path": row["refs"]["state"]}
                for row in child_runs
                if isinstance(row.get("refs"), dict)
            ],
        ],
    }


def _continue_packet_payload(rollup: dict[str, object]) -> dict[str, object]:
    child_pending = [
        f"{row.get('run_id')}: {row.get('status')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict) and _status_bucket(row.get("status")) in _ACTIONABLE_STATUS_BUCKETS
    ]
    progress = rollup.get("task_progress") if isinstance(rollup.get("task_progress"), dict) else {}
    active_items = [item for item in progress.get("active_items", []) if isinstance(item, dict)]
    recent_done = [item for item in progress.get("recent_done_items", []) if isinstance(item, dict)]
    pending = [
        *[
            f"{item.get('id')}: {item.get('status')} - {item.get('title')}"
            for item in active_items
        ],
        *child_pending,
    ]
    next_action = str(progress.get("next_action") or "").strip()
    if not next_action:
        next_action = next(
            (
                str(item.get("next") or "").strip()
                for item in active_items
                if str(item.get("next") or "").strip()
            ),
            "",
        )
    if not next_action and active_items:
        next_action = f"继续处理 task_progress 中的首个未完成项：{active_items[0].get('title') or active_items[0].get('id')}"
    if not next_action:
        next_action = "根据当前任务目标和工作区事实继续推进；不要重复已登记完成的工作。"
    return {
        "schema_version": "continue-packet.v1",
        "scope": "task",
        "next_action": next_action,
        "completed_headings": [str(item.get("title") or item.get("id") or "") for item in recent_done[-12:]],
        "avoid_repeating": [str(item.get("id") or "") for item in recent_done[-12:]],
        "active_refs": [
            str(rollup.get("rollup_json") or ""),
            str(rollup.get("compact_package") or ""),
            str(rollup.get("task_workspace") or ""),
        ],
        "pending_work": pending,
    }


def _root_task_progress(
    task_root: Path,
    task_id: str,
) -> tuple[dict[str, object], dict[str, object] | None]:
    if not task_id or len(task_root.parents) < 3 or task_root.parents[1].name != "tasks":
        return {}, None
    owner_root = task_root.parents[2]
    progress, load_error = read_task_progress_report(owner_root, task_id)
    return task_progress_summary(progress), load_error


def _rollup_markdown(rollup: dict[str, object]) -> str:
    rows = [
        f"- {row.get('run_id')}: {row.get('status')} progress={row.get('progress')} summary={row.get('summary')}"
        for row in rollup.get("child_runs", [])
        if isinstance(row, dict)
    ]
    return (
        "# Task Compact Rollup\n\n"
        f"- task_id: {rollup.get('task_id', '')}\n"
        f"- status: {rollup.get('status', '')}\n"
        f"- child_count: {rollup.get('child_count', 0)}\n"
        f"- status_counts: {json.dumps(rollup.get('status_counts', {}), ensure_ascii=False, sort_keys=True)}\n"
        f"- updated_at: {rollup.get('updated_at', '')}\n\n"
        "## Child Runs\n\n"
        + ("\n".join(rows) if rows else "- 暂无")
        + "\n"
    )


def _read_json_report(path: Path, *, context: str) -> JsonObjectReadReport:
    return read_json_object_report(path, context=context)


def _list_strings(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item)]


def _status_counts(child_runs: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in child_runs:
        status = _status_bucket(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _status_groups(child_runs: list[dict[str, object]]) -> dict[str, list[str]]:
    return {
        "completed": _run_ids_with_status(child_runs, {"done"}),
        "pending": _run_ids_with_status(child_runs, _ACTIONABLE_STATUS_BUCKETS),
        "blocked": _run_ids_with_status(child_runs, _BLOCKED_STATUS_BUCKETS),
    }


def _run_ids_with_status(child_runs: list[dict[str, object]], statuses: frozenset[str] | set[str]) -> list[str]:
    result: list[str] = []
    for row in child_runs:
        if _status_bucket(row.get("status")) in statuses:
            result.append(str(row.get("run_id") or ""))
    return result


def _status_bucket(value: object) -> str:
    # Rollup grouping is derived from the current TaskStatus protocol only.
    try:
        status = normalize_task_status(value)
    except ValueError:
        return "unknown"
    return _STATUS_BUCKETS_BY_PROTOCOL[status]


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _safe_branch_id(value: object) -> str:
    text = str(value or "main").strip() or "main"
    result = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)
    return result.strip("._-") or "main"


__all__ = ["TaskCompactRollupResult", "sync_task_compact_rollup"]
