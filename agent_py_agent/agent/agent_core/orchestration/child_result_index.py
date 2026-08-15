from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...model_visible_refs import current_model_ref, current_model_ref_list
from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_status_in


@dataclass(frozen=True)
class ChildResultNodeRefs:
    registry_refs: list[dict[str, object]]
    expected_outputs: list[str]
    primary_refs: list[str]
    workspace_refs: dict[str, object]
    recovery_refs: dict[str, object]
    progress_layer: dict[str, object]
    final_report_ref: str
    summary_ref: str
    checkpoint_ref: str


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
    status = _task_text(task, "status")
    row: dict[str, object] = {
        "run_id": _task_text(task, "id"),
        "parent_run_id": _task_text(task, "parent_id"),
        "root_run_id": _task_text(task, "root_id") or _task_text(task, "id"),
        "agent_name": _task_text(task, "agent_name"),
        "role": _task_text(task, "role"),
        "status": status,
        "work_scope_key": str(attrs.get("work_scope_key") or ""),
        "expected_outputs": expected_outputs,
        "primary_artifact_refs": primary_artifact_refs,
        "primary_artifact_stats": _artifact_stats(primary_artifact_refs),
        "artifact_registry_refs": artifacts,
        "read_order": _read_order(primary_artifact_refs, ""),
        "task_root": current_model_ref(_task_text(task, "task_workspace_dir")),
    }
    if output_error is not None:
        row["output_load_error"] = runtime_error_report(output_error, context="child_result.output_json")
    return row


def _child_result_node_row(node: dict[str, object]) -> dict[str, object]:
    refs = _child_node_refs(node)
    status = str(node.get("status") or "").strip()
    return {
        "run_id": str(node.get("run_id") or "").strip(),
        "parent_run_id": str(node.get("parent_run_id") or "").strip(),
        "root_run_id": str(node.get("root_run_id") or node.get("root_id") or "").strip(),
        "agent_name": str(node.get("agent_name") or "").strip(),
        "role": str(node.get("role") or "").strip(),
        "status": status,
        "verification_status": str(node.get("verification_status") or "").strip(),
        "work_scope_key": str(node.get("work_scope_key") or "").strip(),
        "expected_outputs": refs.expected_outputs,
        "primary_artifact_refs": refs.primary_refs,
        "primary_artifact_stats": _artifact_stats(refs.primary_refs),
        "artifact_registry_refs": _current_registry_refs(refs.registry_refs),
        "summary_ref": refs.summary_ref,
        "checkpoint_ref": refs.checkpoint_ref,
        "read_order": _read_order(refs.primary_refs, refs.summary_ref if task_status_in(status, {TaskStatus.DONE.value}) else ""),
        "task_root": current_model_ref(refs.workspace_refs.get("task_root")),
        "agent_work_dir": current_model_ref(refs.workspace_refs.get("agent_work_dir")),
        "progress": node.get("progress", 0.0),
        "current_tool": str(node.get("current_tool") or "").strip(),
        "latest_summary": str(node.get("latest_summary") or refs.progress_layer.get("latest_summary") or "").strip(),
        "last_progress_summary": str(
            node.get("last_progress_summary") or refs.progress_layer.get("last_progress_summary") or ""
        ).strip(),
        "not_done_reason": str(node.get("not_done_reason") or "").strip(),
        "recent_tool_trace": _dict_list(node.get("recent_tool_trace"))[-5:],
        "readiness": _readiness_label(str(node.get("status") or ""), refs.primary_refs, refs.summary_ref),
    }


def _child_node_refs(node: dict[str, object]) -> ChildResultNodeRefs:
    registry_refs = _dict_list(node.get("artifact_registry_refs"))
    artifact_refs = _string_items(node.get("artifact_refs"))
    expected_outputs = _node_expected_outputs(node)
    workspace_refs = _dict_field(node, "workspace_refs")
    recovery_refs = _dict_field(node, "recovery_refs")
    progress_layer = _dict_field(node, "progress_layer")
    primary_refs = _node_primary_refs(registry_refs, artifact_refs, expected_outputs)
    return ChildResultNodeRefs(
        registry_refs=registry_refs,
        expected_outputs=expected_outputs,
        primary_refs=primary_refs,
        workspace_refs=workspace_refs,
        recovery_refs=recovery_refs,
        progress_layer=progress_layer,
        final_report_ref=current_model_ref(workspace_refs.get("final_report")),
        summary_ref=current_model_ref(recovery_refs.get("summary")),
        checkpoint_ref=current_model_ref(recovery_refs.get("checkpoint")),
    )


def _node_primary_refs(
    registry_refs: list[dict[str, object]],
    artifact_refs: list[str],
    expected_outputs: list[str],
) -> list[str]:
    primary_refs = [ref for item in registry_refs if (ref := current_model_ref(item.get("path")))]
    primary_refs.extend(current_model_ref_list(artifact_refs))
    primary_refs.extend(ref for ref in expected_outputs if _looks_like_existing_path(ref))
    return list(dict.fromkeys(primary_refs))


def _dict_field(node: dict[str, object], key: str) -> dict[str, object]:
    value = node.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _readiness_label(status: str, primary_refs: list[str], summary_ref: str) -> str:
    if task_status_in(status, {TaskStatus.DONE.value}):
        return "result_ready" if (primary_refs or summary_ref) else "done_without_refs"
    if primary_refs:
        return "partial_artifacts_available"
    if summary_ref:
        return "progress_refs_available"
    if task_status_in(status, {TaskStatus.RUNNING.value, TaskStatus.PLANNING.value}):
        return "running_no_result_yet"
    return "not_ready"


def _expected_outputs(attrs: dict[str, object]) -> list[str]:
    result: list[str] = []
    for key in ("output_files", "output_refs", "artifact_refs"):
        value = attrs.get(key)
        if isinstance(value, list):
            result.extend(current_model_ref_list(value))
    return list(dict.fromkeys(result))


def _node_expected_outputs(node: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("declared_output_refs", "expected_outputs", "output_refs", "output_files", "artifact_refs"):
        refs.extend(current_model_ref_list(_string_items(node.get(key))))
    return list(dict.fromkeys(refs))


def _read_order(
    primary_refs: list[str],
    summary_ref: str,
) -> list[str]:
    refs = [*primary_refs]
    if not refs and summary_ref:
        refs.append(summary_ref)
    return list(dict.fromkeys(refs))


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


def _artifact_stats(paths: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            stat = path.stat()
        except OSError:
            continue
        row: dict[str, object] = {"path": str(raw), "size_bytes": stat.st_size}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            rows.append(row)
            continue
        row["line_count"] = len(text.splitlines())
        row["char_count"] = len(text)
        rows.append(row)
    return rows


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
