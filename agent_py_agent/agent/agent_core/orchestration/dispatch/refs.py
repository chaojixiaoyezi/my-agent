
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ....model_visible_refs import current_model_ref, current_model_ref_list, current_model_text
from ....runtime_errors import runtime_error_report


@dataclass(frozen=True)
class _TaskLoadResult:
    task: object
    error: BaseException | None = None


def related_task_refs(agent: object, report: object, attr: str, *, limit: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for run_id in _related_run_ids(report):
        task = _safe_load_task(agent, run_id)
        task_refs = _task_ref_values(task, attr, limit=limit)
        if _extend_unique_refs(refs, seen, task_refs, limit):
            return refs
    return refs


def related_task_result_refs(agent: object, report: object, *, per_run_artifact_limit: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    run_ids = _related_run_ids(report)
    if not run_ids:
        run_ids, list_error = _visible_run_ids(agent)
        if list_error:
            rows.append(_list_runs_error_row(list_error))
    for run_id in run_ids:
        loaded = _load_task_result(agent, run_id)
        if loaded.error:
            rows.append(_task_load_error_row(run_id, loaded.error))
            continue
        if not _has_task_identity(loaded.task, run_id):
            continue
        row = _task_result_ref_row(loaded.task, per_run_artifact_limit=per_run_artifact_limit)
        if row:
            rows.append(row)
    return rows
def _related_run_ids(report: object) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        _extend_unique_refs(ids, set(ids), _record_related_run_ids(record), 100)
    return ids

def _visible_run_ids(agent: object, *, limit: int = 20) -> tuple[list[str], BaseException | None]:
    try:
        tasks = list(agent.subagents.list_runs())
    except Exception as exc:
        return [], exc
    ids: list[str] = []
    for task in sorted(tasks, key=_task_sort_key):
        run_id = str(getattr(task, "id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
        if len(ids) >= limit:
            break
    return ids, None

def _list_runs_error_row(exc: BaseException) -> dict[str, object]:
    return {
        "run_id": "",
        "status": "LIST_FAILED",
        "summary": "子代理列表读取失败；这不是没有子代理，也不是子代理没有产物。",
        "primary_artifact_ids": [],
        "primary_artifact_refs": [],
        "primary_artifact_registry_refs": [],
        "primary_artifact_summaries": [],
        "evidence_refs": [],
        "load_error": runtime_error_report(exc, context="subagents.list_runs"),
    }

def _load_task_result(agent: object, run_id: str) -> _TaskLoadResult:
    try:
        return _TaskLoadResult(agent.subagents.load(run_id))
    except Exception as exc:
        return _TaskLoadResult(object(), exc)

def _task_load_error_row(run_id: str, exc: BaseException) -> dict[str, object]:
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

def _task_sort_key(task: object) -> tuple[int, float, str]:
    try:
        depth = int(getattr(task, "depth", 0) or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        created = float(getattr(task, "created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    return depth, created, str(getattr(task, "id", "") or "")

def _record_related_run_ids(record: object) -> list[str]:
    run_id = str(getattr(record, "run_id", "") or "").strip()
    return [run_id] if run_id else []

def _extend_unique_refs(target: list[str], seen: set[str], refs: list[str], limit: int) -> bool:
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        target.append(ref)
        if len(target) >= limit:
            return True
    return False

def _safe_load_task(agent: object, run_id: str) -> object:
    try:
        return agent.subagents.load(run_id)
    except Exception:
        return object()

def _has_task_identity(task: object, run_id: str) -> bool:
    return str(getattr(task, "id", "") or "").strip() == run_id

def _task_ref_values(task: object, attr: str, *, limit: int) -> list[str]:
    if attr != "artifact_refs":
        return _string_refs(getattr(task, attr, []), limit=limit)
    output_payload = _read_output_payload(task)
    registry_records = _task_registry_records(task, output_payload, limit=limit)
    registry_paths = _registry_paths(registry_records, limit=limit)
    if registry_paths:
        return registry_paths
    refs = _string_refs(getattr(task, attr, []), limit=limit)
    if refs:
        return refs
    return _output_artifact_refs(output_payload, limit=limit)

def _task_result_ref_row(task: object, *, per_run_artifact_limit: int) -> dict[str, object]:
    output_payload, output_error = _read_output_payload_result(task)
    registry_records = _task_registry_records(task, output_payload, limit=per_run_artifact_limit)
    artifacts = _registry_paths(registry_records, limit=per_run_artifact_limit)
    if not artifacts:
        artifacts = _string_refs(getattr(task, "artifact_refs", []), limit=per_run_artifact_limit)
    if not artifacts:
        artifacts = _output_artifact_refs(output_payload, limit=per_run_artifact_limit)
    evidence = _string_refs(getattr(task, "evidence_refs", []), limit=2)
    summary = _task_summary(task, output_payload)
    row: dict[str, object] = {
        "run_id": str(getattr(task, "id", "") or ""),
        "agent_name": str(getattr(task, "agent_name", "") or ""),
        "role": str(getattr(task, "role", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "verification_status": str(getattr(task, "verification_status", "") or ""),
        "summary": current_model_text(summary),
        "primary_artifact_ids": _registry_ids(registry_records, limit=per_run_artifact_limit),
        "primary_artifact_refs": artifacts,
        "primary_artifact_registry_refs": registry_records,
        "primary_artifact_summaries": _output_artifact_summaries(output_payload, limit=per_run_artifact_limit),
        "evidence_refs": evidence,
        "run_closeout_ref": current_model_ref(getattr(task, "output_json", "")),
        "runner_result_ref": current_model_ref(getattr(task, "runner_result_json", "")),
    }
    if output_error:
        row["output_load_error"] = runtime_error_report(output_error, context="subagent.output_json")
    return row

def _read_output_payload(task: object) -> dict[str, object]:
    return _read_output_payload_result(task)[0]

def _read_output_payload_result(task: object) -> tuple[dict[str, object], BaseException | None]:
    output_json = str(getattr(task, "output_json", "") or "")
    if not output_json:
        return {}, None
    if not Path(output_json).exists():
        return {}, None
    try:
        payload = json.loads(Path(output_json).read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, exc
    return (payload, None) if isinstance(payload, dict) else ({}, None)

def _task_summary(task: object, output_payload: dict[str, object]) -> str:
    candidates = (
        getattr(task, "latest_summary", ""),
        getattr(task, "result", ""),
        output_payload.get("summary", ""),
        output_payload.get("message", ""),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text[:500]
    return _first_packet_claim(output_payload.get("evidence_packets"))[:500]

def _first_packet_claim(value: object) -> str:
    if not isinstance(value, list):
        return ""
    for packet in value:
        claim = _packet_claim(packet)
        if claim:
            return claim
    return ""

def _packet_claim(packet: object) -> str:
    if not isinstance(packet, dict):
        return ""
    return str(packet.get("claim") or "").strip()

def _output_artifact_refs(payload: dict[str, object], *, limit: int) -> list[str]:
    refs = _artifact_entry_paths(payload.get("artifacts"))
    refs.extend(_packet_artifact_refs(payload.get("evidence_packets")))
    return _unique_strings(refs)[:limit]

def _output_artifact_summaries(payload: dict[str, object], *, limit: int) -> list[dict[str, str]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    rows: list[dict[str, str]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = current_model_ref(item.get("path"))
        if not path:
            continue
        rows.append({
            "path": path,
            "kind": str(item.get("kind") or "").strip(),
            "summary": current_model_text(str(item.get("summary") or "").strip()[:220]),
        })
        if len(rows) >= limit:
            break
    return rows

def _task_registry_records(
    task: object,
    output_payload: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    rows = _registry_records_from_attrs(getattr(task, "attributes", {}))
    rows.extend(_registry_records_from_output(output_payload.get("artifacts")))
    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        projected = _current_registry_record(row)
        key = _registry_record_key(projected)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(projected)
        if len(deduped) >= limit:
            break
    return deduped

def _current_registry_record(row: dict[str, object]) -> dict[str, object]:
    if "path" not in row:
        return dict(row)
    path = current_model_ref(row.get("path"))
    if path:
        return {**row, "path": path}
    return {key: value for key, value in row.items() if key != "path"}

def _registry_record_key(row: dict[str, object]) -> str:
    return str(row.get("artifact_id") or row.get("path") or "").strip()

def _registry_records_from_attrs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    records = value.get("artifact_registry_refs")
    if not isinstance(records, list):
        return []
    return [dict(item) for item in records if isinstance(item, dict)]

def _registry_records_from_output(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        ref = item.get("registry_ref")
        if isinstance(ref, dict):
            rows.append(dict(ref))
    return rows

def _registry_paths(records: list[dict[str, object]], *, limit: int) -> list[str]:
    return _unique_strings([current_model_ref(item.get("path")) for item in records])[:limit]

def _registry_ids(records: list[dict[str, object]], *, limit: int) -> list[str]:
    return _unique_strings([str(item.get("artifact_id") or "").strip() for item in records])[:limit]

def _artifact_entry_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(current_model_ref(item.get("path")))
    return [ref for ref in refs if ref]

def _packet_artifact_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            refs.extend(current_model_ref_list(item.get("artifact_refs") or []))
    return [ref for ref in refs if ref]

def _string_refs(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return current_model_ref_list(value, limit=limit)

def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
