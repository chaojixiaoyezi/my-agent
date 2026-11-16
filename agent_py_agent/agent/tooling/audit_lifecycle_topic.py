"""消息生命周期字段白名单；组件已脱敏不代表可公开整个组件对象。"""
from __future__ import annotations

import math

_IDENTIFIERS = frozenset(("call_id", "workflow_run_id", "request_id", "request_ref", "message_ref", "ingress_id", "receipt_id", "operation_id",
    "target_turn_id", "thread_id", "owner_id", "task_id", "task_run_id", "agent_run_id", "attempt_id", "run_id",
    "root_run_id", "round_id", "provider", "model", "model_name", "delivery_outcome", "tool_name", "status", "state", "error_code", "disposition", "reason_code", "provider_ref_hash"))
_NUMBERS = frozenset(("recorded_at", "created_at", "updated_at", "started_at", "ended_at", "duration_ms", "duration_seconds",
    "sent_at", "dispatch_started_at", "dispatch_completed_at", "dispatch_duration_seconds", "message_sequence", "claim_sequence", "tool_rounds", "request_prepare_ms", "pre_call_housekeeping_ms", "supervision_sweep_ms"))
_CONTAINERS = frozenset(("timestamps", "request_stage_timing", "stages_ms", "reply_receipt_summaries", "runtime_authority"))

_TIMING_FIELDS = _NUMBERS | frozenset(("prepare_ms", "queue_wait_ms", "agent_init_ms", "lock_wait_ms", "accepted_at",
    "dispatched_at", "owner_agent_ready_at", "lock_wait_started_at", "lock_acquired_at", "attempt_started_at",
    "chunk_close_ms", "completed_audit_ms", "conversation_prep_ms", "execution_body_ms", "execution_error_ms",
    "execution_ms", "final_request_read_ms", "finalize_ms", "guidance_close_ms", "guidance_release_ms", "lease_stop_ms",
    "request_audit_ms", "request_context_build_ms", "request_json_read_ms", "request_read_ms", "request_runtime_setup_ms",
    "resume_check_ms", "run_ms", "total_ms", "turn_started_ms", "local_store_read_ms", "local_store_write_ms"))


def metadata_only(value):
    """递归仅保留契约 ID、状态码与有限数值；自由文本和未知对象默认丢弃。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in _IDENTIFIERS and isinstance(item, str) and len(item) <= 256 or key in _NUMBERS and type(item) in (int, float) and math.isfinite(item):
            result[key] = item
        elif key in _CONTAINERS:
            if key in ("timestamps", "request_stage_timing", "stages_ms") and isinstance(item, dict):
                result[key] = {name: number for name, number in item.items() if name in _TIMING_FIELDS
                               and type(number) in (int, float) and math.isfinite(number)}
            elif isinstance(item, list):
                result[key] = [metadata_only(row) for row in item[:100] if isinstance(row, dict)]
            elif isinstance(item, dict):
                result[key] = metadata_only(item)
    return result


def lifecycle_topic(agent, query):
    from .audit_requests_topic import _scope_owners, outcome_query, request_reader

    allowed, mapping, scope = _scope_owners(agent, query, 64)
    reader = request_reader(agent, "message_lifecycle_records")
    result = reader(outcome_query(query, allowed, mapping)) if callable(reader) else {
        "entries": [], "coverage": {"status": "unavailable", "reason": "no_gateway_request_context"}}
    for entry in result.get("entries", []):
        entry["runtime"] = _runtime_evidence(agent, entry)
    return {"message_lifecycle": result, **scope, "sources": ["adapter_ingress", "gateway_input_receipts", "gateway_request_records"]}


def _runtime_evidence(agent, entry):
    """只读当前已认证 owner 的原仓储；跨 owner 不猜路径。"""
    import json
    import sqlite3

    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path

    authority = entry.get("runtime_authority") or {}
    owner = str(agent.home_paths.owner_id or "local/main")
    if entry.get("owner_id") != owner:
        return {"coverage": {"status": "unknown", "reason": "owner_runtime_not_bound"}}
    task_id, attempt_id = authority.get("task_id"), authority.get("attempt_id")
    try:
        repo = RuntimeRepository(runtime_db_path(agent.home_paths.owner_home_dir), read_only=True, existing_only=True)
        control = None
        if entry.get("operation_id"):
            control = repo.find_host_command_by_operation(owner_id=owner, operation_id=entry["operation_id"])
            if control is not None:
                authority = {key: getattr(control, key) for key in ("task_id", "agent_run_id", "run_id", "attempt_id")}
                task_id, attempt_id = control.task_id, control.attempt_id
        if not task_id or not attempt_id:
            return {"coverage": {"status": "missing", "reason": "runtime_authority_missing"}}
        task = repo.get_task(task_id)
        attempt = repo.get_attempt(attempt_id)
        if task is None or attempt is None:
            return {"coverage": {"status": "missing", "reason": "runtime_rows_missing"}}
        if dict(task).get("owner_id") != owner:
            return {"coverage": {"status": "unknown", "reason": "runtime_owner_mismatch"}}
        agent_run = repo.get_agent_run(authority.get("agent_run_id", ""))
        if agent_run is None or dict(attempt).get("agent_run_id") != dict(agent_run).get("agent_run_id"):
            return {"coverage": {"status": "unknown", "reason": "runtime_attempt_binding_mismatch"}}
        task_run = repo.get_task_run(dict(agent_run).get("task_run_id", ""))
        if task_run is None or dict(task_run).get("task_id") != task_id:
            return {"coverage": {"status": "unknown", "reason": "runtime_task_binding_mismatch"}}
        result = {"task": metadata_only(dict(task)), "attempt": metadata_only(dict(attempt))}
        if control is not None:
            result["control_operation"] = metadata_only({"operation_id": entry["operation_id"], **authority})
        metadata = json.loads(dict(attempt).get("metadata_json") or "{}")
        result["timing"] = metadata_only(metadata)
        from ..observability.task_call_inspector import TaskCallInspector
        inspector = TaskCallInspector(agent.home_paths.owner_home_dir, owner_id=owner)
        reader = getattr(inspector, "get_task_call_page", None)
        if callable(reader):
            page = reader(task_id, limit=20, cursor="")
            result["model_rounds"] = [metadata_only(row) for row in page.get("rows", [])]
            result["model_coverage"] = page.get("coverage", {"status": "unknown"})
            result["next_cursor"] = page.get("next_cursor")
        else:
            result["model_coverage"] = {"status": "unavailable", "reason": "model_reader_unavailable"}
        return result
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        return {"coverage": {"status": "unavailable", "reason": type(error).__name__}}
