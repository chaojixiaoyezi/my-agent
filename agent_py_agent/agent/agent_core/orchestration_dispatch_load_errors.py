"""Helpers for exposing child task load failures without hiding them as empty results."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class TaskLoadResult:
    task: object
    error: BaseException | None = None


def load_task_result(agent: object, run_id: str) -> TaskLoadResult:
    try:
        return TaskLoadResult(agent.subagents.load(run_id))
    except Exception as exc:
        return TaskLoadResult(object(), exc)


def task_load_error_row(run_id: str, exc: BaseException) -> dict[str, object]:
    return {
        "run_id": str(run_id or ""),
        "status": "LOAD_FAILED",
        "summary": "子代理账本读取失败；这不是子代理无产物。",
        "primary_artifact_ids": [],
        "primary_artifact_refs": [],
        "primary_artifact_registry_refs": [],
        "primary_artifact_summaries": [],
        "evidence_refs": [],
        "load_error": runtime_error_report(exc, context="subagents.load"),
    }
