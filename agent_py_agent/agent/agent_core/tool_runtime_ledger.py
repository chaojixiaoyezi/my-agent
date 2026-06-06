
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.gates.tool.effects import args_hash_for_call
from ..contracts.tool_protocol_v2 import normalize_tool_call
from ..local_storage import RuntimeGateLedgerRecord
from ..local_storage.control_plane_models import AgentEventInput
from .tool_guard.call_guardrail import tool_guardrail_policy, tool_guardrail_records


def persist_tool_runtime_ledger(agent: object, archive_record: dict[str, object]) -> None:
    store = getattr(agent, "local_store", None)
    _best_effort_control_plane_write(lambda: _record_tool_agent_event(store, archive_record))
    if not hasattr(store, "record_runtime_gate_ledger"):
        return
    record = runtime_gate_ledger_record_from_archive(archive_record)
    if record is None:
        return
    _best_effort_control_plane_write(lambda: store.record_runtime_gate_ledger(record))


def _best_effort_control_plane_write(write_fn) -> None:
    try:
        write_fn()
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
    except OSError:
        return


def _record_tool_agent_event(store: object, archive_record: dict[str, object]) -> None:
    if not hasattr(store, "record_agent_event"):
        return
    run_id = _text(archive_record.get("run_id"))
    if not run_id:
        return
    scope = _dict_value(archive_record.get("run_scope"))
    root_task_id = (
        _text(scope.get("root_task_id"))
        or _text(archive_record.get("root_task_id"))
        or _text(archive_record.get("task_id"))
        or run_id
    )
    event = AgentEventInput(
        root_task_id=root_task_id,
        run_id=run_id,
        parent_run_id=_text(scope.get("parent_run_id")) or _text(archive_record.get("parent_run_id")),
        event_type="tool_call_finished",
        payload=_tool_event_payload(archive_record, scope),
    )
    store.record_agent_event(event)


def _tool_event_payload(
    archive_record: dict[str, object],
    scope: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool": _text(archive_record.get("tool")),
        "ok": bool(archive_record.get("ok")),
        "call_id": _text(archive_record.get("call_id")),
        "operation_id": _operation_id(archive_record),
        "scope": dict(scope),
    }
    for key in ("error_code", "error_category", "recommended_action", "result_ref"):
        value = _text(archive_record.get(key))
        if value:
            payload[key] = value
    refs = archive_record.get("tool_result_refs")
    if isinstance(refs, list):
        payload["tool_result_refs"] = [item for item in refs if isinstance(item, dict)]
    return payload


def write_boundary_with_runtime_ledger(agent: object, params: object) -> dict[str, object] | None:
    boundary = getattr(params, "write_boundary", None)
    merged = dict(boundary) if isinstance(boundary, dict) else {}
    _attach_task_workspace_aliases(merged, params)
    guardrail_rows = tool_guardrail_records(agent)
    if guardrail_rows:
        merged["tool_guardrail_records"] = _merged_tool_guardrail_rows(
            merged.get("tool_guardrail_records"),
            guardrail_rows,
        )
    guardrail_policy = tool_guardrail_policy(params)
    if guardrail_policy:
        merged["tool_guardrail_policy"] = guardrail_policy
    store = getattr(agent, "local_store", None)
    if not hasattr(store, "runtime_idempotency_ledger"):
        return merged or boundary
    run_id = _text(getattr(params, "run_id", ""))
    if not run_id:
        return merged or boundary
    persisted = store.runtime_idempotency_ledger(run_id=run_id)
    if persisted:
        merged["idempotency_ledger"] = _merged_idempotency_rows(merged.get("idempotency_ledger"), persisted)
    rate_rows = _runtime_tool_rate_limit_rows(store, run_id)
    if rate_rows:
        merged["tool_rate_limit_records"] = _merged_rate_limit_rows(merged.get("tool_rate_limit_records"), rate_rows)
    return merged or boundary


def _attach_task_workspace_aliases(boundary: dict[str, object], params: object) -> None:
    workspace = _run_workspace(params)
    if not workspace:
        return
    for source_key, target_key in (
        ("task_root", "task_root"),
        ("output_dir", "task_output_dir"),
        ("work_dir", "task_work_dir"),
        ("user_requested_output_dir", "user_requested_output_dir"),
    ):
        text = _text(workspace.get(source_key))
        if text and not _text(boundary.get(target_key)):
            boundary[target_key] = text
    for key in ("output_dir", "work_dir"):
        text = _text(workspace.get(key))
        if text:
            _append_boundary_path(boundary, "allowed_write_roots", text)


def _run_workspace(params: object) -> dict[str, object]:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and isinstance(attrs.get("run_workspace"), dict):
        return dict(attrs["run_workspace"])
    contract = getattr(params, "delivery_contract", None)
    if isinstance(contract, dict) and isinstance(contract.get("task_workspace"), dict):
        return dict(contract["task_workspace"])
    return {}


def _append_boundary_path(boundary: dict[str, object], key: str, path: str) -> None:
    existing = boundary.get(key)
    values = [str(item) for item in existing] if isinstance(existing, list) else []
    if path not in values:
        values.append(path)
    boundary[key] = values


def runtime_gate_ledger_record_from_archive(archive_record: dict[str, object]) -> RuntimeGateLedgerRecord | None:
    runtime_gate = archive_record.get("runtime_gate")
    if not isinstance(runtime_gate, dict):
        return None
    run_id = _text(archive_record.get("run_id"))
    operation_id = _operation_id(archive_record)
    if not run_id or not operation_id:
        return None
    return RuntimeGateLedgerRecord(
        run_id=run_id,
        task_id=_text(archive_record.get("task_id")),
        operation_id=operation_id,
        tool=_text(archive_record.get("tool")),
        parameters=_dict_value(archive_record.get("parameters")),
        runtime_gate=runtime_gate,
        idempotency_key=_idempotency_key(archive_record, runtime_gate),
        args_hash=_args_hash(archive_record),
        approval_id=_approval_id(archive_record, runtime_gate),
        result_ref=_result_ref(archive_record),
        status=_ledger_status(archive_record, runtime_gate),
    )


def _operation_id(record: Mapping[str, object]) -> str:
    direct = _text(record.get("operation_id"))
    if direct:
        return direct
    protocol = _dict_value(record.get("tool_protocol_v2"))
    return _text(protocol.get("operation_id"))


def _idempotency_key(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("idempotency_key"))
    if direct:
        return direct
    protocol = _dict_value(record.get("tool_protocol_v2"))
    protocol_key = _text(protocol.get("idempotency_key"))
    if protocol_key:
        return protocol_key
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("idempotency_key"))


def _approval_id(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("approval_id"))
    if direct:
        return direct
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("approval_id"))


def _args_hash(record: Mapping[str, object]) -> str:
    parameters = _dict_value(record.get("parameters"))
    if not parameters:
        return ""
    call = normalize_tool_call(parameters)
    return args_hash_for_call(call.input)


def _result_ref(record: Mapping[str, object]) -> str:
    for key in ("artifact_ref", "output_path", "source_ref", "path"):
        value = _text(record.get(key))
        if value:
            return value
    return ""


def _ledger_status(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    if runtime_gate.get("allowed") is not True:
        return "blocked"
    return "done" if record.get("ok") is True else "failed"


def _merged_idempotency_rows(existing: object, persisted: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *persisted]:
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("idempotency_key"))
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "idempotency_key": key,
            "args_hash": _text(item.get("args_hash")),
            "status": _text(item.get("status")),
            "result_ref": _text(item.get("result_ref")),
        })
    return tuple(rows)


def _runtime_tool_rate_limit_rows(store: object, run_id: str) -> tuple[dict[str, object], ...]:
    if not hasattr(store, "list_runtime_gate_ledger"):
        return ()
    records = store.list_runtime_gate_ledger(run_id=run_id, limit=500)
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        identity = _rate_limit_identity(record)
        if identity is None:
            continue
        row = by_identity.setdefault(identity, _new_rate_limit_row(identity))
        _apply_rate_limit_record(row, record)
    return tuple(by_identity.values())


def _rate_limit_identity(record: object) -> tuple[str, str] | None:
    tool = _text(getattr(record, "tool", ""))
    args_hash = _text(getattr(record, "args_hash", ""))
    return (tool, args_hash) if tool and args_hash else None


def _new_rate_limit_row(identity: tuple[str, str]) -> dict[str, object]:
    tool, args_hash = identity
    return {
        "tool_name": tool,
        "args_hash": args_hash,
        "attempt_timestamps": [],
        "consecutive_failures": 0,
        "last_failure_at": 0.0,
        "last_success_at": 0.0,
        "total_failures": 0,
    }


def _apply_rate_limit_record(row: dict[str, object], record: object) -> None:
    timestamp = float(getattr(record, "created_at", 0.0) or getattr(record, "updated_at", 0.0) or 0.0)
    if timestamp > 0:
        row["attempt_timestamps"].append(timestamp)
    status = _text(getattr(record, "status", "")).lower()
    if status == "failed":
        row["consecutive_failures"] = int(row["consecutive_failures"]) + 1
        row["total_failures"] = int(row["total_failures"]) + 1
        row["last_failure_at"] = timestamp
    if status == "done":
        row["consecutive_failures"] = 0
        row["last_success_at"] = timestamp


def _merged_rate_limit_rows(existing: object, persisted: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *persisted]:
        if not isinstance(item, Mapping):
            continue
        tool = _text(item.get("tool_name") or item.get("tool"))
        args_hash = _text(item.get("args_hash"))
        if not tool or not args_hash or (tool, args_hash) in seen:
            continue
        seen.add((tool, args_hash))
        rows.append(dict(item))
    return tuple(rows)


def _merged_tool_guardrail_rows(
    existing: object,
    runtime_rows: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for item in [*(existing if isinstance(existing, (list, tuple)) else ()), *runtime_rows]:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return tuple(rows[-256:])


def _dict_value(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}

__all__ = [
    "persist_tool_runtime_ledger",
    "runtime_gate_ledger_record_from_archive",
    "write_boundary_with_runtime_ledger",
]
