
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
    payload = _approved_fact_payload(request)
    (root / "task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return str(root)


def _runtime_fact_payload(request: RuntimeFactSourceRequest) -> dict[str, Any]:
    sections = _explicit_sections(_fact_source_text(request))
    latest_tests = dedupe_strings([*sections.tests, *_tool_test_items(request.archive_tool_calls)])
    desired_outputs = runtime_desired_outputs(request.delivery_contract, request.runtime_injections)
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
        "acceptance": sections.acceptance,
        "constraints": sections.constraints,
        "latest_tests": latest_tests,
        "desired_outputs": desired_outputs,
        "run_intent": run_intent,
        "runtime_progress": _runtime_progress_payload(request),
        "run_status": {
            "status": request.status,
            "backend": request.backend,
            "response_present": bool(request.response_text.strip()),
        },
    }


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
    lowered = str(status or "").strip().lower()
    if lowered in {"ok", "succeeded", "done"}:
        return "final"
    if lowered in {"failed", "timeout", "interrupted"}:
        return lowered
    return "running"


def _fact_source_text(request: RuntimeFactSourceRequest) -> str:
    continuation_blocks = [
        text for text in _runtime_injection_texts(request.runtime_injections) if "# Compact Auto Continuation" in text
    ]
    return "\n\n".join([request.user_prompt, *continuation_blocks])


def _runtime_injection_texts(value: tuple[str, ...] | list[str] | str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _approved_fact_payload(request: ApprovedRuntimeFactSourceRequest) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "approved_runtime_fact_source",
        "fact_id": request.fact_id,
        "source_apply_id": request.source_apply_id,
        "goal": request.goal.strip(),
        "next_actions": dedupe_strings(request.next_actions),
        "acceptance": dedupe_strings(request.acceptance),
        "constraints": dedupe_strings(request.constraints),
        "latest_tests": dedupe_strings(request.latest_tests),
        "run_status": {
            "status": "approved_manual_completion",
            "backend": "manual",
            "response_present": False,
        },
    }


@dataclass(frozen=True)
class _ExplicitSections:
    acceptance: list[str]
    constraints: list[str]
    tests: list[str]


def _explicit_sections(text: str) -> _ExplicitSections:
    lines = text.splitlines()
    buckets = {"acceptance": [], "constraints": [], "tests": []}
    active = ""
    for line in lines:
        label, inline = _section_heading(line)
        if label:
            active = label
            buckets[label].extend(_inline_items(inline))
            continue
        if active and _is_section_metadata_line(line):
            continue
        if _looks_like_unmatched_heading(line):
            active = ""
            continue
        if active:
            buckets[active].extend(_line_items(line))
    return _ExplicitSections(
        acceptance=dedupe_strings(buckets["acceptance"]),
        constraints=dedupe_strings(buckets["constraints"]),
        tests=dedupe_strings(buckets["tests"]),
    )


def _is_section_metadata_line(line: str) -> bool:
    text = line.strip().lower()
    return bool(re.match(r"^(status|source_status|source paths?|source_paths)\s*[:：]", text))


def _section_heading(line: str) -> tuple[str, str]:
    text = line.strip().lstrip("-*# ").strip()
    match = re.match(r"^(验收条件|验收|acceptance|constraints?|约束|限制|tests?|测试|最近测试)\s*[:：]\s*(.*)$", text, re.I)
    if not match:
        return _markdown_section_heading(line, text)
    return _label_key(match.group(1)), match.group(2).strip()


def _markdown_section_heading(line: str, text: str) -> tuple[str, str]:
    if not line.strip().startswith("#"):
        return "", ""
    lowered = text.lower()
    if lowered in {"acceptance", "验收", "验收条件"}:
        return "acceptance", ""
    if lowered in {"constraints", "constraint", "约束", "限制"}:
        return "constraints", ""
    if lowered in {"latest tests", "tests", "test", "最近测试", "测试"}:
        return "tests", ""
    return "", ""


def _looks_like_unmatched_heading(line: str) -> bool:
    if line.strip().startswith("#"):
        return True
    text = line.strip().lstrip("-*# ").strip()
    if not text:
        return False
    if re.match(r"^[^:：]{1,40}\s*[:：]\s*$", text):
        return True
    return bool(re.match(r"^[^:：]{1,40}\s*[:：]\s+.+$", text))


def _label_key(label: str) -> str:
    lower = label.lower()
    if lower in {"acceptance", "验收条件", "验收"}:
        return "acceptance"
    if lower in {"constraint", "constraints", "约束", "限制"}:
        return "constraints"
    return "tests"


def _line_items(line: str) -> list[str]:
    text = line.strip()
    for prefix in ("- [x]", "- [X]", "- [ ]", "- ", "* "):
        if text.startswith(prefix):
            item = text[len(prefix) :].strip()
            return [item] if item else []
    return []


def _inline_items(text: str) -> list[str]:
    if not text:
        return []
    return [item.strip() for item in re.split(r"[;；]", text) if item.strip()]


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
