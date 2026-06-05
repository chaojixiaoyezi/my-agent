
"""Build compact work-state inputs from bounded task/run fact sources.

The scanner only reads known fact files under the current workspace and scoped
task/run/agent roots. Missing acceptance, constraints, or tests are represented
as ``not_recorded`` so compact resume can continue ordinary work without
inventing requirements or scanning unrelated project files.
"""

from __future__ import annotations

"""bounded fact-source scanner for compact work-state snapshots."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import dedupe_strings
from ...subagents.services.agent_run_state import read_agent_state_payload
from ...task_progress import progress_path, read_task_progress_report, task_progress_summary
from ..compact_runtime_handoff import build_runtime_handoff
from .run_intent_sources import compact_run_intent_payload

_ACCEPTANCE_FILES = ("ACCEPTANCE.md", "acceptance.md")
_CONSTRAINT_FILES = ("CONSTRAINTS.md", "constraints.md")
_TEST_FILES = ("TEST_CHECKLIST.md", "test_checklist.md", "failing_tests.json", "next_actions.json")


@dataclass(frozen=True)
class WorkStateFieldSourceRequest:
    plan: dict[str, Any]
    source_state: dict[str, Any]


@dataclass(frozen=True)
class WorkStateFieldSources:
    goal: str
    next_actions: list[str]
    acceptance: dict[str, Any]
    constraints: dict[str, Any]
    latest_tests: dict[str, Any]
    read_files: list[str]
    task_progress: dict[str, Any]
    runtime_handoff: dict[str, Any]
    desired_outputs: dict[str, Any]
    run_intent: dict[str, Any]
    target_coverage: dict[str, Any]


def build_work_state_field_sources(request: WorkStateFieldSourceRequest) -> WorkStateFieldSources:
    roots = _candidate_fact_roots(request)
    ids = _scoped_ids(request)
    workspace = Path(str(request.plan["workspace_root"]))
    goal = _first_item(_field_items(roots, ("task.json",), json_keys=("goal",)))
    next_actions = _field_items(roots, ("task.json", "next_actions.json"), json_keys=("next_actions",))
    acceptance = _field_payload(_field_items(roots, _ACCEPTANCE_FILES, json_keys=("acceptance_checks", "acceptance")))
    constraints = _field_payload(_field_items(roots, _CONSTRAINT_FILES, json_keys=("constraints", "hard_constraints")))
    latest_tests = _test_payload(
        _field_items(roots, _TEST_FILES, json_keys=("latest_tests", "tests", "failing_tests", "test_status"))
    )
    read_files = dedupe_strings([*acceptance["source_paths"], *constraints["source_paths"], *latest_tests["source_paths"]])
    task_progress = _first_task_progress([workspace, *roots], ids)
    runtime_handoff = build_runtime_handoff(workspace, ids)
    desired_outputs = _field_payload(_field_items(roots, ("task.json",), json_keys=("desired_outputs",)))
    run_intent = compact_run_intent_payload(roots)
    target_coverage = _object_payload(_field_objects(roots, ("task.json",), json_keys=("target_coverage",)))
    return WorkStateFieldSources(
        goal,
        list(next_actions["items"]),
        acceptance,
        constraints,
        latest_tests,
        read_files,
        task_progress,
        runtime_handoff,
        desired_outputs,
        run_intent,
        target_coverage,
    )


def _candidate_fact_roots(request: WorkStateFieldSourceRequest) -> list[Path]:
    workspace = Path(str(request.plan["workspace_root"]))
    ids = _scoped_ids(request)
    roots = [_path_root(workspace, item) for item in request.source_state["content_paths"]]
    for item_id in ids:
        roots.extend(_id_roots(workspace, item_id))
    return _existing_dirs(_dedupe_paths(roots), workspace)


def _scoped_ids(request: WorkStateFieldSourceRequest) -> list[str]:
    scope = request.plan.get("scope", {}) if isinstance(request.plan.get("scope"), dict) else {}
    return dedupe_strings([
        str(scope.get("request_id") or ""),
        str(scope.get("session_id") or ""),
        str(scope.get("task_id") or ""),
        str(scope.get("run_id") or ""),
        *request.source_state["task_refs"],
    ])


def _first_task_progress(roots: list[Path], ids: list[str]) -> dict[str, Any]:
    roots = _dedupe_paths(roots)
    for root, item_id, progress in _task_progress_candidates(roots, ids):
        if _task_progress_has_content(progress):
            return task_progress_summary({**progress, "ref": str(progress_path(root, item_id))})
    return {}


def _task_progress_candidates(roots: list[Path], ids: list[str]) -> list[tuple[Path, str, dict[str, Any]]]:
    return [(root, item_id, read_task_progress_report(root, item_id)[0]) for item_id in ids for root in roots]


def _task_progress_has_content(progress: dict[str, Any]) -> bool:
    return bool(progress.get("load_error") or progress["summary"] or progress["next_action"] or progress["counts"].get("total", 0))


def _id_roots(workspace: Path, item_id: str) -> list[Path]:
    if not item_id:
        return []
    task_agent_roots = [
        path
        for pattern in ("*/work/agents/*", "*/agents/*")
        for path in sorted((workspace / "tasks").glob(pattern))
        if path.is_dir() and path.name == item_id
    ]
    return [
        workspace / "subagents" / item_id,
        workspace / "tasks" / item_id,
        workspace / "memory_archive" / "runtime_facts" / _safe_runtime_fact_id(item_id),
        *task_agent_roots,
    ]


def _safe_runtime_fact_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


def _field_items(roots: list[Path], file_names: tuple[str, ...], *, json_keys: tuple[str, ...]) -> dict[str, Any]:
    parsed = [_parsed_source(path, json_keys) for root in roots for path in _candidate_files(root, file_names)]
    parsed.extend(_parsed_source(root / "task.json", json_keys) for root in roots if (root / "task.json").exists())
    return {
        "items": dedupe_strings([item for items, _path in parsed for item in items]),
        "source_paths": dedupe_strings([path for items, path in parsed if items and path]),
    }


def _parsed_source(path: Path, json_keys: tuple[str, ...]) -> tuple[list[str], str]:
    return (_items_from_file(path, json_keys), str(path))


def _field_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": list(source["items"]),
        "source_status": "recorded" if source["items"] else "not_recorded",
        "source_paths": list(source["source_paths"]),
    }


def _field_objects(roots: list[Path], file_names: tuple[str, ...], *, json_keys: tuple[str, ...]) -> dict[str, Any]:
    parsed = [_parsed_object(path, json_keys) for root in roots for path in _candidate_files(root, file_names)]
    parsed.extend(_parsed_object(root / "task.json", json_keys) for root in roots if (root / "task.json").exists())
    objects = [item for item, _path in parsed if item]
    return {
        "objects": objects,
        "source_paths": dedupe_strings([path for item, path in parsed if item and path]),
    }


def _parsed_object(path: Path, json_keys: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    payload = _read_json_dict(path)
    for key in json_keys:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return dict(value), str(path)
    return {}, str(path)


def _object_payload(source: dict[str, Any]) -> dict[str, Any]:
    objects = source.get("objects") if isinstance(source.get("objects"), list) else []
    payload = dict(objects[0]) if objects and isinstance(objects[0], dict) else {}
    return {
        "payload": payload,
        "source_status": "recorded" if payload else "not_recorded",
        "source_paths": list(source["source_paths"]),
    }


def _first_item(source: dict[str, Any]) -> str:
    return next((item for item in source["items"] if item), "")


def _test_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "recorded" if source["items"] else "not_recorded",
        "items": list(source["items"]),
        "source_paths": list(source["source_paths"]),
    }


def _candidate_files(root: Path, file_names: tuple[str, ...]) -> list[Path]:
    return [path for name in file_names if (path := root / name).exists() and path.is_file()]


def _items_from_file(path: Path, json_keys: tuple[str, ...]) -> list[str]:
    if path.suffix.lower() == ".json":
        return _items_from_json(path, json_keys)
    return _items_from_markdown(path)


def _items_from_markdown(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [item for line in lines if (item := _strip_markdown_item(line))]


def _items_from_json(path: Path, keys: tuple[str, ...]) -> list[str]:
    payload = _read_json_dict(path)
    values = [item for key in keys if key in payload for item in _value_items(payload[key])]
    if not values and path.name == "failing_tests.json":
        values.extend(_value_items(payload))
    return values


def _value_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [item for raw in value for item in _value_items(raw)]
    if isinstance(value, dict):
        return [f"{key}: {_compact_value(item)}" for key, item in value.items() if _compact_value(item)]
    return [str(value).strip()] if value not in (None, "") else []


def _strip_markdown_item(line: str) -> str:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("```"):
        return ""
    for prefix in ("- [x]", "- [X]", "- [ ]", "- ", "* "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text if len(text) <= 160 else ""


def _compact_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _path_root(workspace: Path, value: str) -> Path:
    path = Path(value)
    candidate = path if path.is_absolute() else workspace / path
    try:
        resolved = candidate.resolve()
        workspace_resolved = workspace.resolve()
    except OSError:
        return workspace
    if workspace_resolved not in (resolved, *resolved.parents):
        return workspace
    return resolved if resolved.is_dir() else resolved.parent


def _existing_dirs(paths: list[Path], workspace: Path) -> list[Path]:
    return [path for path in paths if path.exists() and path.is_dir() and _inside_workspace(path, workspace)]


def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        resolved = path.resolve()
        root = workspace.resolve()
    except OSError:
        return False
    return root in (resolved, *resolved.parents)


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = read_agent_state_payload(path) if path.name == "task.json" else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, FileNotFoundError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dedupe_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


__all__ = ["WorkStateFieldSourceRequest", "WorkStateFieldSources", "build_work_state_field_sources"]
