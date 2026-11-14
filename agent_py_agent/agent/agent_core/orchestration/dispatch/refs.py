
from __future__ import annotations

import inspect
import json
from pathlib import Path

from ....model_visible_refs import current_model_ref, current_model_ref_list


def exact_dispatch_tasks(manager, run_ids, *, allow_missing=False) -> list:
    """规范文件仍是权威；显式 include 的不存在项留给原诊断，不吞损坏和未知。"""
    from ....subagents.services.persistence.run_index import SubagentRunIndexUnavailable

    report = manager.list_runs_by_ids_report(run_ids)
    errors = [item for item in report.load_errors
              if not (allow_missing and item.get("error_type") == "FileNotFoundError")]
    if errors:
        error = SubagentRunIndexUnavailable("dispatch canonical candidates unavailable")
        error.load_errors = errors
        raise error
    return report.runs


def dispatch_scope_tasks(agent, ctx=None, *, limit: int = 0) -> list:
    """按原宿主 scope 先取索引 ID；无请求的显式历史入口保留原列表合同。"""
    from ....subagents.manager import SubAgentManager, creation_run_index, request_scope_runs
    from ....subagents.services.persistence.run_index import request_selection
    from ..create_policy import current_orchestration_requester_run_id
    from ..run_scope import remembered_orchestration_run_ids

    manager = agent.subagents
    if (not isinstance(manager, SubAgentManager)
            or getattr(manager.list_runs, "__func__", None) is not SubAgentManager.list_runs):
        return manager.list_runs()
    included = [str(item).strip() for item in (getattr(ctx, "include_run_ids", None) or []) if str(item).strip()]
    if included:
        return exact_dispatch_tasks(manager, included, allow_missing=True)
    parent, root = (str(getattr(ctx, key, "") or "").strip() for key in ("parent_run_id", "root_id"))
    key = root or parent or current_orchestration_requester_run_id(agent)
    if not key:
        remembered = remembered_orchestration_run_ids(agent)
        if not remembered:
            return manager.list_runs()
        tasks = exact_dispatch_tasks(manager, sorted(remembered))
        return request_scope_runs(manager, {task.root_id or task.id for task in tasks}, "", None)
    selection = ({key}, "", None) if root or parent else request_selection(agent, key)
    if limit <= 0:
        return request_scope_runs(manager, *selection)
    identity = manager.persistence._run_index_identity()
    index = creation_run_index(manager)
    ids = _limited_child_ids(index, key, limit, selection[2])
    manager.persistence._require_run_index_identity(identity)
    tasks = exact_dispatch_tasks(manager, ids)
    _verify_dispatch_scope(tasks, key, selection[2])
    return tasks


def _limited_child_ids(index, parent_id: str, limit: int, epoch) -> list[str]:
    """展示从原 parent 投影限量，未决义务优先；不读孙代理或其它请求正文。"""
    with index.connect() as db:
        db.execute("BEGIN")
        index._require_complete(db)
        return [row[0] for row in db.execute(
            "SELECT l.run_id FROM run_lookup l JOIN runs r ON r.run_id=l.run_id WHERE l.parent_id=? "
            "AND (? IS NULL OR r.audit_epoch=?) "
            "ORDER BY EXISTS (SELECT 1 FROM membership m WHERE m.scope_id='' AND m.kind='capability' "
            "AND m.run_id=l.run_id) DESC,l.cover_open DESC,r.updated DESC,l.run_id LIMIT ?",
            (parent_id, epoch, epoch, limit))]


def _verify_dispatch_scope(tasks, parent_id, epoch) -> None:
    from ....subagents.services.persistence.run_index import SubagentRunIndexUnavailable

    for task in tasks:
        attrs = task.attributes if isinstance(task.attributes, dict) else {}
        try:
            current_epoch = max(0, int(attrs.get("audit_run_epoch") or 0))
        except (ValueError, TypeError):
            current_epoch = None
        if task.parent_id != parent_id or epoch is not None and epoch != current_epoch:
            raise SubagentRunIndexUnavailable("dispatch index canonical relation changed")


def candidate_ids_kwargs(method, run_ids: list[str]) -> dict:
    """C-BOARD 并行集成期间只给支持该 keyword 的原方法传精确来源，执行错误不降级。"""
    parameters = inspect.signature(method).parameters.values()
    if any(item.name == "candidate_ids" or item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
        return {"candidate_ids": run_ids}
    return {}


def related_task_refs(agent: object, report: object, attr: str, *, limit: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for run_id in _related_run_ids(report):
        task = _safe_load_task(agent, run_id)
        task_refs = _task_ref_values(task, attr, limit=limit)
        if _extend_unique_refs(refs, seen, task_refs, limit):
            return refs
    return refs


def _related_run_ids(report: object) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        _extend_unique_refs(ids, set(ids), _record_related_run_ids(record), 100)
    return ids

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

def _output_artifact_refs(payload: dict[str, object], *, limit: int) -> list[str]:
    refs = _artifact_entry_paths(payload.get("artifacts"))
    refs.extend(_packet_artifact_refs(payload.get("evidence_packets")))
    return _unique_strings(refs)[:limit]

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
