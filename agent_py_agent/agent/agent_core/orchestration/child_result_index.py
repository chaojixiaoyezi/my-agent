from __future__ import annotations

import json
from pathlib import Path

from ...model_visible_refs import current_model_ref, current_model_ref_list
from ...runtime_errors import runtime_error_report


def child_result_index(agent: object, tasks: list[object]) -> list[dict[str, object]]:
    """Return refs-first child delivery rows without reading artifact bodies."""
    del agent
    return [_child_result_row(task) for task in tasks if _task_text(task, "id")]


def child_result_index_from_nodes(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the same compact index from inspect_agent_tree node rows."""
    return [_child_result_node_row(node) for node in nodes if str(node.get("run_id") or "").strip()]


def _child_result_row(task: object) -> dict[str, object]:
    attrs = _task_attrs(task)
    artifacts = _artifact_registry_refs(task)
    expected_outputs = _expected_outputs(attrs)
    output_payload, output_error = _output_payload(task)
    primary_artifact_refs = _primary_artifact_refs(output_payload, artifacts, expected_outputs)
    row: dict[str, object] = {
        "run_id": _task_text(task, "id"),
        "parent_run_id": _task_text(task, "parent_id"),
        "root_run_id": _task_text(task, "root_id") or _task_text(task, "id"),
        "agent_name": _task_text(task, "agent_name"),
        "role": _task_text(task, "role"),
        "status": _task_text(task, "status"),
        "work_scope_key": str(attrs.get("work_scope_key") or ""),
        "expected_outputs": expected_outputs,
        "primary_artifact_refs": primary_artifact_refs,
        "artifact_registry_refs": artifacts,
        "final_report_ref": current_model_ref(_task_text(task, "agent_run_final_report_md") or _task_text(task, "output_json")),
        "task_root": current_model_ref(_task_text(task, "task_workspace_dir")),
    }
    if output_error is not None:
        row["output_load_error"] = runtime_error_report(output_error, context="child_result.output_json")
    return row


def _child_result_node_row(node: dict[str, object]) -> dict[str, object]:
    registry_refs = _dict_list(node.get("artifact_registry_refs"))
    artifact_refs = _string_items(node.get("artifact_refs"))
    workspace_refs = dict(node.get("workspace_refs") or {}) if isinstance(node.get("workspace_refs"), dict) else {}
    primary_refs = [ref for item in registry_refs if (ref := current_model_ref(item.get("path")))]
    primary_refs.extend(current_model_ref_list(artifact_refs))
    return {
        "run_id": str(node.get("run_id") or "").strip(),
        "parent_run_id": str(node.get("parent_run_id") or "").strip(),
        "root_run_id": str(node.get("root_run_id") or node.get("root_id") or "").strip(),
        "agent_name": str(node.get("agent_name") or "").strip(),
        "role": str(node.get("role") or "").strip(),
        "status": str(node.get("status") or "").strip(),
        "verification_status": str(node.get("verification_status") or "").strip(),
        "work_scope_key": str(node.get("work_scope_key") or "").strip(),
        "expected_outputs": [],
        "primary_artifact_refs": list(dict.fromkeys(primary_refs)),
        "artifact_registry_refs": _current_registry_refs(registry_refs),
        "final_report_ref": "",
        "task_root": current_model_ref(workspace_refs.get("task_root")),
    }


def _expected_outputs(attrs: dict[str, object]) -> list[str]:
    result: list[str] = []
    for key in ("output_files", "output_refs", "artifact_refs"):
        value = attrs.get(key)
        if isinstance(value, list):
            result.extend(current_model_ref_list(value, basename_for_legacy=True))
    return list(dict.fromkeys(result))


def _primary_artifact_refs(
    output: dict[str, object],
    artifacts: list[dict[str, object]],
    expected_outputs: list[str],
) -> list[str]:
    refs: list[str] = []
    for row in artifacts:
        path = current_model_ref(row.get("path"))
        if path:
            refs.append(path)
    refs.extend(_artifact_paths_from_output(output))
    for path in expected_outputs:
        if _looks_like_existing_path(path):
            refs.append(path)
    return list(dict.fromkeys(refs))


def _artifact_paths_from_output(output: dict[str, object]) -> list[str]:
    items = output.get("artifacts")
    if not isinstance(items, list):
        return []
    return [path for path in (_artifact_item_path(item) for item in items) if path]


def _artifact_item_path(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return current_model_ref(item.get("path"))


def _artifact_registry_refs(task: object, *, limit: int = 12) -> list[dict[str, object]]:
    attrs = _task_attrs(task)
    value = attrs.get("artifact_registry_refs")
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        key = str(row.get("artifact_id") or row.get("path") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(_current_registry_row(row))
        if len(rows) >= limit:
            break
    return rows


def _output_payload(task: object) -> tuple[dict[str, object], BaseException | None]:
    path = Path(_task_text(task, "output_json"))
    if not path.is_file():
        return {}, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, exc
    return (value if isinstance(value, dict) else {}), None


def _task_attrs(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    return dict(attrs) if isinstance(attrs, dict) else {}


def _task_text(task: object, field: str) -> str:
    value = getattr(task, field, "")
    return value if isinstance(value, str) else str(value or "").strip()


def _looks_like_existing_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        return Path(text).expanduser().is_file()
    except OSError:
        return False


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _current_registry_refs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [_current_registry_row(item) for item in rows]


def _current_registry_row(item: dict[str, object]) -> dict[str, object]:
    row = dict(item)
    if "path" in row:
        path = current_model_ref(row.get("path"))
        if path:
            row["path"] = path
        else:
            row.pop("path", None)
    return row


__all__ = ["child_result_index", "child_result_index_from_nodes"]
