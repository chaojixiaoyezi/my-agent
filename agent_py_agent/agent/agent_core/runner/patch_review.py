
from __future__ import annotations

"""Patch-review candidate helpers for runner dispatch."""

import json
from pathlib import Path

from ...subagent import SubAgentTask


def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:
    run_ids: list[str] = []
    for task in tasks:
        if task.status != "DONE":
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


def _task_has_runner_patches(task: SubAgentTask) -> bool:
    try:
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload.get("patches"), list) and bool(payload.get("patches"))
