
from __future__ import annotations

"""Refs-only budget summaries for subagent runner activity."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report


@dataclass(frozen=True)
class SubagentRunBudgetRequest:
    """Request bundle for a subagent run budget report."""

    manager: Any
    root_id: str = ""
    max_model_calls: int = 0
    max_tool_rounds: int = 0
    max_prompt_response_tokens: int = 0
    include_dry_runs: bool = False
    reserved: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentRunBudgetRecord:
    """Per-run budget line item."""

    run_id: str
    status: str
    dry_run: bool
    backend: str
    tool_rounds: int
    prompt_token_estimate: int
    response_token_estimate: int
    prompt_file: str
    response_file: str
    result_json: str


@dataclass(frozen=True)
class SubagentRunBudgetReport:
    """Budget summary for a subagent run tree."""

    generated_at: float
    root_id: str
    totals: dict[str, int]
    limits: dict[str, int]
    exceeded: list[str]
    records: list[SubagentRunBudgetRecord] = field(default_factory=list)
    load_errors: list[dict[str, object]] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)


def build_subagent_run_budget_report(request: SubagentRunBudgetRequest) -> SubagentRunBudgetReport:
    """Build a refs-only budget report from persisted subagent runner results."""

    pairs = [_budget_record_report(task) for task in _scoped_tasks(request)]
    records = [record for record, _load_error in pairs if record is not None]
    load_errors = [load_error for _record, load_error in pairs if load_error is not None]
    if not request.include_dry_runs:
        records = [record for record in records if not record.dry_run]
    totals = _totals(records)
    limits = _limits(request)
    return SubagentRunBudgetReport(
        generated_at=time.time(),
        root_id=str(request.root_id or ""),
        totals=totals,
        limits=limits,
        exceeded=_exceeded(totals, limits),
        records=records,
        load_errors=load_errors,
        reserved=dict(request.reserved or {}),
    )


def _scoped_tasks(request: SubagentRunBudgetRequest) -> list[Any]:
    root_id = str(request.root_id or "").strip()
    tasks = list(request.manager.list_runs())
    if not root_id:
        return tasks
    return [task for task in tasks if (str(getattr(task, "root_id", "") or getattr(task, "id", ""))) == root_id]


def _budget_record(task: Any) -> SubagentRunBudgetRecord | None:
    record, _load_error = _budget_record_report(task)
    return record


def _budget_record_report(task: Any) -> tuple[SubagentRunBudgetRecord | None, dict[str, object] | None]:
    result_path = Path(str(getattr(task, "runner_result_json", "") or ""))
    if not result_path.is_file():
        return None, None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, _runner_result_load_error(task, result_path, exc)
    if not isinstance(payload, dict):
        return None, _runner_result_load_error(
            task,
            result_path,
            ValueError(f"runner_result_json is {type(payload).__name__}, expected object"),
        )
    prompt_file = str(payload.get("prompt_file") or "")
    response_file = str(payload.get("response_file") or "")
    return SubagentRunBudgetRecord(
        run_id=str(payload.get("run_id") or getattr(task, "id", "")),
        status=str(payload.get("status") or getattr(task, "status", "")),
        dry_run=bool(payload.get("dry_run", False)),
        backend=str(payload.get("backend") or ""),
        tool_rounds=int(payload.get("tool_rounds") or 0),
        prompt_token_estimate=_file_token_estimate(prompt_file),
        response_token_estimate=_file_token_estimate(response_file),
        prompt_file=prompt_file,
        response_file=response_file,
        result_json=str(result_path),
    ), None


def _runner_result_load_error(task: Any, path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="subagent.run_budget.runner_result")
    report["run_id"] = str(getattr(task, "id", "") or "")
    report["path"] = str(path)
    return report


def _file_token_estimate(path: str) -> int:
    if not path:
        return 0
    try:
        size = Path(path).stat().st_size
    except OSError:
        return 0
    return max(1, int(size / 4)) if size else 0


def _totals(records: list[SubagentRunBudgetRecord]) -> dict[str, int]:
    return {
        "runs": len(records),
        "model_calls": sum(1 for record in records if not record.dry_run),
        "tool_rounds": sum(record.tool_rounds for record in records),
        "prompt_token_estimate": sum(record.prompt_token_estimate for record in records),
        "response_token_estimate": sum(record.response_token_estimate for record in records),
        "prompt_response_token_estimate": sum(
            record.prompt_token_estimate + record.response_token_estimate for record in records
        ),
    }


def _limits(request: SubagentRunBudgetRequest) -> dict[str, int]:
    return {
        "model_calls": max(0, int(request.max_model_calls or 0)),
        "tool_rounds": max(0, int(request.max_tool_rounds or 0)),
        "prompt_response_token_estimate": max(0, int(request.max_prompt_response_tokens or 0)),
    }


def _exceeded(totals: dict[str, int], limits: dict[str, int]) -> list[str]:
    return [key for key, limit in limits.items() if limit > 0 and totals.get(key, 0) > limit]
