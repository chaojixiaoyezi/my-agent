from __future__ import annotations

"""Stable, low-cost material state fingerprints for automatic supervision."""

import hashlib
import json
from dataclasses import asdict, is_dataclass


def subagent_material_signature(
    agent: object,
    *,
    task_id: str,
    watched_run_ids: object = None,
) -> str:
    """Return a hash of user-relevant task changes; return empty on load failure."""
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return ""
    try:
        if callable(getattr(manager, "list_runs_report", None)):
            report = manager.list_runs_report()
            if list(getattr(report, "load_errors", []) or []):
                return ""
            tasks = list(getattr(report, "runs", []) or [])
        else:
            tasks = list(manager.list_runs())
    except Exception:
        return ""
    selected = _selected_run_ids(tasks, task_id=task_id, watched_run_ids=watched_run_ids)
    rows = [_material_row(task) for task in tasks if str(getattr(task, "id", "") or "") in selected]
    rows.sort(key=lambda row: str(row.get("run_id") or ""))
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _selected_run_ids(tasks: list[object], *, task_id: str, watched_run_ids: object) -> set[str]:
    selected = {str(task_id or "").strip()}
    if isinstance(watched_run_ids, (list, tuple, set)):
        selected.update(str(item or "").strip() for item in watched_run_ids)
    selected.discard("")
    changed = True
    while changed:
        changed = False
        for task in tasks:
            run_id = str(getattr(task, "id", "") or "").strip()
            parent_id = str(getattr(task, "parent_id", "") or "").strip()
            root_id = str(getattr(task, "root_id", "") or "").strip()
            if run_id and run_id not in selected and (parent_id in selected or root_id in selected):
                selected.add(run_id)
                changed = True
    return selected


def _material_row(task: object) -> dict[str, object]:
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "parent_id": str(getattr(task, "parent_id", "") or ""),
        "root_id": str(getattr(task, "root_id", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "verification_status": str(getattr(task, "verification_status", "") or ""),
        "failure_type": str(getattr(task, "failure_type", "") or ""),
        "progress": _number(getattr(task, "progress", 0.0)),
        "current_step": str(getattr(task, "current_step", "") or ""),
        "last_progress_at": _number(getattr(task, "last_progress_at", 0.0)),
        "last_progress_summary": str(getattr(task, "last_progress_summary", "") or ""),
        "blockers": _sorted_strings(getattr(task, "blockers", [])),
        "artifact_refs": _sorted_strings(getattr(task, "artifact_refs", [])),
        "evidence_refs": _sorted_strings(getattr(task, "evidence_refs", [])),
        "capability_requests": _capability_request_rows(getattr(task, "capability_requests", [])),
        "result_sha256": _text_digest(getattr(task, "result", "")),
    }


def _capability_request_rows(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        payload = asdict(item) if is_dataclass(item) else item
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                key: payload.get(key)
                for key in ("request_id", "capability", "kind", "status", "decision")
                if key in payload
            }
        )
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _sorted_strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted(str(item) for item in value)


def _number(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _text_digest(value: object) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


__all__ = ["subagent_material_signature"]
