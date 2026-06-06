
from __future__ import annotations

"""run-local fact source writer for compact/resume."""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.value_parsing import dedupe_strings
from ..run_intent import build_run_intent, run_intent_payload
from .runtime_workspace_outputs import run_intent_has_desired_outputs, runtime_desired_outputs


@dataclass(frozen=True)
class RuntimeFactSourceRequest:
    root: Path
    request_id: str
    user_prompt: str = ""
    response_text: str = ""
    backend: str = ""
    status: str = "running"
    next_actions: list[str] = field(default_factory=list)
    archive_tool_calls: list[Any] = field(default_factory=list)
    runtime_injections: tuple[str, ...] = ()
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    phase: str = ""
    tool_rounds: int = 0
    executed_tools: list[str] = field(default_factory=list)
    latest_archive_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    delivery_contract: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApprovedRuntimeFactSourceRequest:
    root: Path
    fact_id: str
    goal: str
    next_actions: list[str]
    acceptance: list[str]
    constraints: list[str]
    latest_tests: list[str]
    source_apply_id: str = ""


def write_runtime_fact_source(request: RuntimeFactSourceRequest) -> str:
    if not request.request_id:
        return ""
    root = request.root / "memory_archive" / "runtime_facts" / _safe_id(request.request_id)
    root.mkdir(parents=True, exist_ok=True)
    payload = _runtime_fact_payload(request)
    _write_json_atomic(root / "task.json", payload)
    return str(root)


def write_approved_runtime_fact_source(request: ApprovedRuntimeFactSourceRequest) -> str:
    if not request.fact_id:
        return ""
    root = request.root / "memory_archive" / "runtime_facts" / _safe_id(request.fact_id)
    root.mkdir(parents=True, exist_ok=True)
    payload = _approved_fact_payload(request, _read_json_dict(root / "task.json"))
    _write_json_atomic(root / "task.json", payload)
    return str(root)


def _runtime_fact_payload(request: RuntimeFactSourceRequest) -> dict[str, Any]:
    latest_tests = dedupe_strings(_tool_test_items(request.archive_tool_calls))
    desired_outputs = runtime_desired_outputs(request.delivery_contract, request.runtime_injections)
    delivery_contract = _delivery_contract_payload(request.delivery_contract)
    target_coverage = _target_coverage_payload(request.delivery_contract)
    run_intent = build_run_intent(
        user_prompt=request.user_prompt,
        delivery_contract=request.delivery_contract,
        workspace_root=request.root,
    )
    if desired_outputs and not run_intent_has_desired_outputs(run_intent):
        run_intent = run_intent_payload(
            reference_roots=[],
            desired_outputs=[item["target_path"] for item in desired_outputs if item.get("target_path")],
        )
    return {
        "version": 1,
        "source": "runtime_fact_source",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "goal": request.user_prompt.strip(),
        "next_actions": list(request.next_actions),
        "acceptance": [],
        "constraints": [],
        "latest_tests": latest_tests,
        "desired_outputs": desired_outputs,
        "delivery_contract": delivery_contract,
        "target_coverage": target_coverage,
        "run_intent": run_intent,
        "runtime_progress": _runtime_progress_payload(request),
        "run_status": {
            "status": request.status,
            "backend": request.backend,
            "response_present": bool(request.response_text.strip()),
        },
    }


def _delivery_contract_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _target_coverage_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    coverage = value.get("target_coverage_contract")
    return dict(coverage) if isinstance(coverage, dict) else {}


def _runtime_progress_payload(request: RuntimeFactSourceRequest) -> dict[str, Any]:
    return {
        "phase": request.phase or _phase_from_status(request.status),
        "source": request.source,
        "tool_rounds": max(0, int(request.tool_rounds or 0)),
        "executed_tools": dedupe_strings([str(item) for item in request.executed_tools if str(item).strip()])[-20:],
        "latest_archive_refs": dedupe_strings(request.latest_archive_refs)[-20:],
        "artifact_refs": dedupe_strings(request.artifact_refs)[-20:],
        "updated_at": _utc_timestamp(),
    }


def _phase_from_status(status: str) -> str:
    normalized = str(status or "").strip().upper()
    if normalized == "DONE":
        return "final"
    if normalized in {"FAILED", "TIMEOUT", "INTERRUPTED"}:
        return normalized.lower()
    return "running"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _approved_fact_payload(request: ApprovedRuntimeFactSourceRequest, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous if isinstance(previous, dict) else {}
    payload = _preserved_runtime_fields(previous)
    payload.update({
        "version": 1,
        "source": "approved_runtime_fact_source",
        "fact_id": request.fact_id,
        "source_apply_id": request.source_apply_id,
        "goal": request.goal.strip() or str(previous.get("goal") or "").strip(),
        "next_actions": _merged_string_items(previous.get("next_actions"), request.next_actions),
        "acceptance": _merged_string_items(previous.get("acceptance"), request.acceptance),
        "constraints": _merged_string_items(previous.get("constraints"), request.constraints),
        "latest_tests": _merged_string_items(previous.get("latest_tests"), request.latest_tests),
        "run_status": {
            "status": "approved_manual_completion",
            "backend": "manual",
            "response_present": False,
        },
    })
    return payload


def _preserved_runtime_fields(previous: dict[str, Any]) -> dict[str, Any]:
    preserved: dict[str, Any] = {}
    for key in (
        "request_id",
        "run_id",
        "task_id",
        "desired_outputs",
        "delivery_contract",
        "target_coverage",
        "run_intent",
        "runtime_progress",
    ):
        if key in previous:
            preserved[key] = previous[key]
    return preserved


def _merged_string_items(previous: Any, approved: list[str]) -> list[str]:
    return dedupe_strings([*_value_strings(previous), *approved])


def _value_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [item for raw in value for item in _value_strings(raw)]
    return []


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_test_items(tool_calls: list[Any]) -> list[str]:
    return [item for call in tool_calls if (item := _tool_test_item(call))]


def _tool_test_item(call: Any) -> str:
    text = json.dumps(call, ensure_ascii=False, sort_keys=True) if isinstance(call, dict) else str(call)
    if not _looks_like_test_command(text):
        return ""
    return text[:240]


def _looks_like_test_command(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("pytest", "unittest", "ruff check", "npm test", "cargo test"))


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


__all__ = [
    "ApprovedRuntimeFactSourceRequest",
    "RuntimeFactSourceRequest",
    "write_approved_runtime_fact_source",
    "write_runtime_fact_source",
]
