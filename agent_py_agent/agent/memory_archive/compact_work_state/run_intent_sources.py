"""Run-intent extraction for compact work-state snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...common.value_parsing import dedupe_strings
from ...run_intent import run_intent_payload
from ...runtime_errors import DataCorruptionError, runtime_error_report
from ...subagents.services.agent_run_state import read_agent_state_payload


def compact_run_intent_payload(roots: list[Path]) -> dict[str, Any]:
    reference_roots: list[str] = []
    desired_outputs: list[str] = []
    load_errors: list[dict[str, Any]] = []
    for root in roots:
        intent, load_error = _run_intent_from_task_json_report(root / "task.json")
        if load_error:
            load_errors.append(load_error)
        reference_roots.extend(_payload_items(intent.get("reference_roots")))
        desired_outputs.extend(_payload_items(intent.get("desired_outputs")))
    payload = run_intent_payload(reference_roots=dedupe_strings(reference_roots), desired_outputs=dedupe_strings(desired_outputs))
    if load_errors:
        payload["load_errors"] = load_errors
    return payload


def _run_intent_from_task_json_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    payload, load_error = _read_task_json_payload_report(path)
    if load_error:
        return {}, load_error
    intent = payload.get("run_intent") if isinstance(payload, dict) else None
    return (intent if isinstance(intent, dict) else {}), None


def _read_task_json_payload_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        payload = read_agent_state_payload(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, _task_json_load_error(exc, path)
    if not isinstance(payload, dict):
        return {}, _task_json_load_error(DataCorruptionError(f"task.json root must be a JSON object: {path}"), path)
    return payload, None


def _task_json_load_error(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(exc, context="compact_work_state.run_intent.task_json")
    report["path"] = str(path)
    return report


def _payload_items(value: Any) -> list[str]:
    payload = value if isinstance(value, dict) else {}
    items = payload.get("items")
    if not isinstance(items, list | tuple):
        return []
    return [text for item in items if (text := str(item).strip())]


__all__ = ["compact_run_intent_payload"]
