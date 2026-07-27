from __future__ import annotations

from pathlib import Path


def dispatch_record_payload(item) -> dict[str, object]:
    payload = {
        "step": item.step,
        "action": item.action,
        "run_id": item.run_id,
        "ok": item.ok,
        "record_applied": item.applied,
        "message": item.message,
        "before_status": item.before_status,
        "after_status": item.after_status,
    }
    payload.update(_dispatch_record_runner_payload(item))
    return payload


def dispatch_recovery_payload(records: list[object]) -> dict[str, object]:
    for item in records:
        if item.step == "runner_selection" and item.action == "invalid_run_ids":
            return {
                "action": "retry_dispatch_with_valid_run_id",
                "message": item.message,
                "valid_run_ids": _valid_run_ids_from_refs(item.evidence_paths or []),
                "valid_task_refs": list(item.evidence_paths or [])[:20],
            }
    return {}


def _dispatch_record_runner_payload(item) -> dict[str, object]:
    keys = {
        "runner_summary": "runner_summary",
        "runner_created_child_count": "runner_created_child_count",
        "runner_created_child_ids": "runner_created_child_ids",
        "runner_created_roles": "runner_created_roles",
        "runner_child_status_counts": "runner_child_status_counts",
        "runner_unfinished_child_ids": "runner_unfinished_child_ids",
        "runner_child_load_errors": "runner_child_load_errors",
        "runner_partial_success": "runner_partial_success",
    }
    return {
        name: value
        for name, attr in keys.items()
        if (value := getattr(item, attr, "")) not in ("", 0, [], None)
    }


def _valid_run_ids_from_refs(paths: list[str]) -> list[str]:
    ids: list[str] = []
    for path in paths[:20]:
        run_id = Path(str(path)).name.strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids
