from __future__ import annotations

"""Task-local artifact ledger projection for subagent run workspaces."""

from typing import Any


def agent_run_artifact_records(
    paths: Any,
    task: Any,
    task_id: str,
    now: float,
) -> list[dict[str, object]]:
    run_id = str(getattr(task, "id", ""))
    records: list[dict[str, object]] = [
        {
            "version": 1,
            "kind": "final_report",
            "task_id": task_id,
            "run_id": run_id,
            "path": str(paths.final_report_md),
            "exists": paths.final_report_md.exists(),
            "scope": "task_local_subagent",
            "updated_at": now,
        }
    ]
    for kind, refs in (
        ("artifact_ref", list(getattr(task, "artifact_refs", []) or [])),
        ("evidence_ref", list(getattr(task, "evidence_refs", []) or [])),
    ):
        records.extend(_ref_records(kind, refs, {"task_id": task_id, "run_id": run_id, "updated_at": now}))
    return records


def _ref_records(kind: str, refs: list[object], context: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "version": 1,
            "kind": kind,
            "task_id": context["task_id"],
            "run_id": context["run_id"],
            "ref": str(ref),
            "scope": "task_local_subagent",
            "updated_at": context["updated_at"],
        }
        for ref in refs
    ]


__all__ = ["agent_run_artifact_records"]
