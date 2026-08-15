from __future__ import annotations

"""Authoritative, owner-scoped tool operation state.

The runtime-gate ledger is observability: losing one audit row must not crash a
task.  This store is different.  A side-effecting tool must atomically claim an
operation here before its implementation is called, and an unavailable store
therefore fails closed.
"""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL_OPERATION_RUNNING = "running"
TOOL_OPERATION_SUCCEEDED = "succeeded"
TOOL_OPERATION_FAILED = "failed"
TOOL_OPERATION_UNKNOWN = "unknown"
TOOL_OPERATION_CANCELLED = "cancelled"
TOOL_OPERATION_TERMINAL_STATUSES = frozenset(
    {
        TOOL_OPERATION_SUCCEEDED,
        TOOL_OPERATION_FAILED,
        TOOL_OPERATION_CANCELLED,
    }
)
TOOL_OPERATION_IDEMPOTENCY_SCOPES = frozenset({"operation", "business"})

_PROCESS_START_TOKEN = ""
_PROCESS_INSTANCE_ID = uuid.uuid4().hex


@dataclass(frozen=True)
class ToolOperationHolder:
    holder_id: str
    host: str
    pid: int
    process_start_token: str


@dataclass(frozen=True)
class ToolOperationClaimRequest:
    owner_id: str
    run_id: str
    task_id: str
    operation_id: str
    tool: str
    args_hash: str
    idempotency_key: str
    idempotency_scope: str
    idempotency_namespace: str
    holder: ToolOperationHolder
    lease_expires_at: float
    now: float = 0.0
    resource_scopes: tuple[str, ...] = ()
    # seq 245 P2：调用者 attempt fence。ToolCall.attempt_id 是宿主注入的可信
    # 身份，claim 用它同事务 CAS current_attempt_id（takeover 后旧 attempt 的
    # 调用被拦截）；空 = 非主链直调（LOCAL/本地兼容），不做 CAS。
    attempt_id: str = ""


@dataclass(frozen=True)
class ToolOperationRecord:
    owner_id: str
    run_id: str
    task_id: str
    operation_id: str
    tool: str
    args_hash: str
    idempotency_key: str
    idempotency_scope: str
    idempotency_namespace: str
    status: str
    holder_id: str
    holder_host: str
    holder_pid: int
    holder_process_start_token: str
    generation: int
    lease_expires_at: float
    result: dict[str, Any] = field(default_factory=dict)
    result_ref: str = ""
    error_code: str = ""
    unknown_reason: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0


@dataclass(frozen=True)
class ToolOperationClaim:
    action: str
    record: ToolOperationRecord
    reason: str = ""


@dataclass(frozen=True)
class ToolOperationCompletionRequest:
    owner_id: str
    run_id: str
    operation_id: str
    holder_id: str
    generation: int
    status: str
    result: dict[str, Any]
    error_code: str = ""
    unknown_reason: str = ""
    now: float | None = None


@dataclass(frozen=True)
class ToolOperationReopenRequest:
    owner_id: str
    run_id: str
    operation_id: str
    expected_generation: int
    holder: ToolOperationHolder
    lease_expires_at: float
    source_ref: str
    reconciliation_result: dict[str, Any] = field(default_factory=dict)
    now: float | None = None


class ToolOperationStateError(RuntimeError):
    """The authoritative operation store is missing or inconsistent."""


class ToolOperationOwnershipError(ToolOperationStateError):
    """A completion did not belong to the current claim holder/generation."""


class LocalStoreToolOperationMixin:
    """Atomic tool-operation claims backed by the owner LocalStore database."""

    def claim_tool_operation(
        self,
        request: ToolOperationClaimRequest,
    ) -> ToolOperationClaim:
        _validate_claim_request(request)
        now = float(request.now or time.time())
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = _select_claim_identity(conn, request)
            if existing is None:
                unresolved = _select_equivalent_unknown_operation(conn, request)
                if unresolved is not None:
                    conn.commit()
                    return ToolOperationClaim(
                        "unknown",
                        unresolved,
                        f"equivalent_unknown_operation:{unresolved.operation_id}",
                    )
                _insert_running_operation(conn, request, now)
                record = _select_operation(
                    conn,
                    request.owner_id,
                    request.run_id,
                    request.operation_id,
                )
                if record is None:
                    raise ToolOperationStateError("tool operation claim was not persisted")
                conn.commit()
                return ToolOperationClaim("execute", record)
            decision = _existing_claim_decision(conn, existing, request, now)
            conn.commit()
            return decision

    def finish_tool_operation(
        self,
        request: ToolOperationCompletionRequest,
    ) -> ToolOperationRecord:
        if request.status not in (
            TOOL_OPERATION_TERMINAL_STATUSES | {TOOL_OPERATION_UNKNOWN}
        ):
            raise ValueError(
                f"invalid settled tool operation status: {request.status}"
            )
        current = float(
            request.now if request.now is not None else time.time()
        )
        encoded = _json_dumps(request.result)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = _update_operation_completion(
                conn,
                request,
                current=current,
                encoded_result=encoded,
            )
            record = _select_operation(
                conn,
                request.owner_id,
                request.run_id,
                request.operation_id,
            )
            if not updated:
                if (
                    record is not None
                    and record.status == request.status
                    and record.holder_id == request.holder_id
                    and record.generation == request.generation
                    and _json_dumps(record.result) == encoded
                ):
                    conn.commit()
                    return record
                raise ToolOperationOwnershipError(
                    "tool operation completion lost claim ownership"
                )
            if record is None:
                raise ToolOperationStateError(
                    "tool operation disappeared after completion"
                )
            conn.commit()
            return record

    # LLM: 只有目标系统证明 not_started，或 provider 声明同一幂等键可安全重放，才可原子重开 unknown。
    # 函数用途: 在同一业务键和同一参数身份上换新 holder/generation，供当前调用安全重试一次。
    def reopen_tool_operation_after_reconciliation(
        self,
        request: ToolOperationReopenRequest,
    ) -> ToolOperationRecord:
        if not str(request.source_ref or "").strip():
            raise ValueError("tool operation reconciliation source_ref is required")
        current = float(
            request.now if request.now is not None else time.time()
        )
        if float(request.lease_expires_at or 0) <= current:
            raise ValueError("reopened tool operation lease must expire in the future")
        encoded_reconciliation = _json_dumps(request.reconciliation_result)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE tool_operations
                SET status = 'running',
                    holder_id = ?, holder_host = ?, holder_pid = ?,
                    holder_process_start_token = ?,
                    generation = generation + 1,
                    lease_expires_at = ?,
                    result_json = ?, error_code = '', unknown_reason = '',
                    updated_at = ?, completed_at = 0
                WHERE owner_id = ? AND run_id = ? AND operation_id = ?
                  AND status = 'unknown' AND generation = ?
                """,
                (
                    request.holder.holder_id,
                    request.holder.host,
                    request.holder.pid,
                    request.holder.process_start_token,
                    float(request.lease_expires_at),
                    encoded_reconciliation,
                    current,
                    request.owner_id,
                    request.run_id,
                    request.operation_id,
                    int(request.expected_generation),
                ),
            )
            record = _select_operation(
                conn,
                request.owner_id,
                request.run_id,
                request.operation_id,
            )
            if cursor.rowcount <= 0 or record is None:
                raise ToolOperationOwnershipError(
                    "tool operation reconciliation lost unknown generation"
                )
            conn.commit()
            return record

    def get_tool_operation(
        self,
        *,
        owner_id: str,
        run_id: str,
        operation_id: str,
    ) -> ToolOperationRecord | None:
        with self._connection() as conn:
            return _select_operation(conn, owner_id, run_id, operation_id)

    def list_tool_operations(
        self,
        *,
        owner_id: str = "",
        run_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[ToolOperationRecord]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("owner_id", owner_id),
            ("run_id", run_id),
            ("status", status),
        ):
            text = str(value or "").strip()
            if text:
                clauses.append(f"{column} = ?")
                values.append(text)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM tool_operations
                {where}
                ORDER BY created_at ASC, operation_id ASC
                LIMIT ?
                """,
                [*values, int(limit)],
            ).fetchall()
        return [_record_from_row(row) for row in rows]


def new_tool_operation_holder() -> ToolOperationHolder:
    host = socket.gethostname()
    pid = os.getpid()
    start_token = _process_start_token()
    return ToolOperationHolder(
        holder_id=f"{host}:{pid}:{start_token or 'x'}:{_PROCESS_INSTANCE_ID}:{uuid.uuid4().hex}",
        host=host,
        pid=pid,
        process_start_token=start_token,
    )


def _validate_claim_request(request: ToolOperationClaimRequest) -> None:
    required = {
        "owner_id": request.owner_id,
        "run_id": request.run_id,
        "operation_id": request.operation_id,
        "tool": request.tool,
        "args_hash": request.args_hash,
        "idempotency_key": request.idempotency_key,
        "idempotency_namespace": request.idempotency_namespace,
        "holder_id": request.holder.holder_id,
    }
    missing = sorted(name for name, value in required.items() if not str(value or "").strip())
    if missing:
        raise ValueError(f"tool operation claim missing fields: {','.join(missing)}")
    if request.idempotency_scope not in TOOL_OPERATION_IDEMPOTENCY_SCOPES:
        raise ValueError(
            f"invalid tool operation idempotency scope: {request.idempotency_scope}"
        )
    if float(request.lease_expires_at or 0) <= float(request.now or time.time()):
        raise ValueError("tool operation lease must expire in the future")


def _select_claim_identity(conn: Any, request: ToolOperationClaimRequest) -> ToolOperationRecord | None:
    direct = _select_operation(
        conn,
        request.owner_id,
        request.run_id,
        request.operation_id,
    )
    if direct is not None or request.idempotency_scope != "business":
        return direct
    row = conn.execute(
        """
        SELECT * FROM tool_operations
        WHERE owner_id = ? AND idempotency_scope = 'business'
          AND idempotency_namespace = ? AND idempotency_key = ?
        """,
        (
            request.owner_id,
            request.idempotency_namespace,
            request.idempotency_key,
        ),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


# LLM: A fresh model call id must not turn the same unresolved operation into permission to run
# again. Business-scoped tools already deduplicate by their declared key; operation-scoped tools
# get this narrower same-run, exact-arguments barrier only while the earlier outcome is unknown.
# 函数用途: 在同一运行中查找参数完全相同但终态未知的副作用操作，阻止换 call_id 盲重试。
def _select_equivalent_unknown_operation(
    conn: Any,
    request: ToolOperationClaimRequest,
) -> ToolOperationRecord | None:
    if request.idempotency_scope != "operation":
        return None
    row = conn.execute(
        """
        SELECT * FROM tool_operations
        WHERE owner_id = ? AND run_id = ? AND task_id = ?
          AND idempotency_scope = 'operation'
          AND idempotency_namespace = ? AND args_hash = ?
          AND status = 'unknown'
        ORDER BY created_at ASC, operation_id ASC
        LIMIT 1
        """,
        (
            request.owner_id,
            request.run_id,
            request.task_id,
            request.idempotency_namespace,
            request.args_hash,
        ),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def _insert_running_operation(
    conn: Any,
    request: ToolOperationClaimRequest,
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO tool_operations(
            owner_id, run_id, task_id, operation_id, tool, args_hash,
            idempotency_key, idempotency_scope, idempotency_namespace,
            status, holder_id, holder_host, holder_pid,
            holder_process_start_token, generation, lease_expires_at,
            result_json, error_code, unknown_reason,
            created_at, updated_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 1, ?,
                '{}', '', '', ?, ?, 0)
        """,
        (
            request.owner_id,
            request.run_id,
            request.task_id,
            request.operation_id,
            request.tool,
            request.args_hash,
            request.idempotency_key,
            request.idempotency_scope,
            request.idempotency_namespace,
            request.holder.holder_id,
            request.holder.host,
            request.holder.pid,
            request.holder.process_start_token,
            float(request.lease_expires_at),
            now,
            now,
        ),
    )


def _existing_claim_decision(
    conn: Any,
    record: ToolOperationRecord,
    request: ToolOperationClaimRequest,
    now: float,
) -> ToolOperationClaim:
    mismatch = _claim_mismatch_reason(record, request)
    if mismatch:
        return ToolOperationClaim("conflict", record, mismatch)
    if record.status in TOOL_OPERATION_TERMINAL_STATUSES:
        return ToolOperationClaim("replay", record)
    if record.status == TOOL_OPERATION_UNKNOWN:
        return ToolOperationClaim("unknown", record, record.unknown_reason)
    if record.status != TOOL_OPERATION_RUNNING:
        return ToolOperationClaim(
            "conflict",
            record,
            f"unsupported persisted status: {record.status}",
        )
    if _operation_holder_is_live(record, now):
        return ToolOperationClaim("in_flight", record)
    reason = (
        "operation lease expired before a terminal result was persisted"
        if record.lease_expires_at <= now
        else "operation owner process is no longer alive"
    )
    updated = _mark_operation_unknown(conn, record, reason=reason, now=now)
    return ToolOperationClaim("unknown", updated or record, reason)


def _mark_operation_unknown(
    conn: Any,
    record: ToolOperationRecord,
    *,
    reason: str,
    now: float,
) -> ToolOperationRecord | None:
    conn.execute(
        """
        UPDATE tool_operations
        SET status = 'unknown', unknown_reason = ?, updated_at = ?
        WHERE owner_id = ? AND run_id = ? AND operation_id = ?
          AND status = 'running' AND generation = ?
        """,
        (
            reason,
            now,
            record.owner_id,
            record.run_id,
            record.operation_id,
            record.generation,
        ),
    )
    return _select_operation(
        conn,
        record.owner_id,
        record.run_id,
        record.operation_id,
    )


def _update_operation_completion(
    conn: Any,
    request: ToolOperationCompletionRequest,
    *,
    current: float,
    encoded_result: str,
) -> bool:
    unknown_reason = (
        str(request.unknown_reason or "")
        if request.status == TOOL_OPERATION_UNKNOWN
        else ""
    )
    completed_at = 0.0 if request.status == TOOL_OPERATION_UNKNOWN else current
    cursor = conn.execute(
        """
        UPDATE tool_operations
        SET status = ?, result_json = ?, error_code = ?,
            unknown_reason = ?, updated_at = ?, completed_at = ?
        WHERE owner_id = ? AND run_id = ? AND operation_id = ?
          AND holder_id = ? AND generation = ?
          AND status IN ('running', 'unknown')
        """,
        (
            request.status,
            encoded_result,
            str(request.error_code or ""),
            unknown_reason,
            current,
            completed_at,
            request.owner_id,
            request.run_id,
            request.operation_id,
            request.holder_id,
            int(request.generation),
        ),
    )
    return cursor.rowcount > 0


def _claim_mismatch_reason(
    record: ToolOperationRecord,
    request: ToolOperationClaimRequest,
) -> str:
    comparisons = (
        ("tool", record.tool, request.tool),
        ("args_hash", record.args_hash, request.args_hash),
        ("idempotency_scope", record.idempotency_scope, request.idempotency_scope),
        (
            "idempotency_namespace",
            record.idempotency_namespace,
            request.idempotency_namespace,
        ),
        ("idempotency_key", record.idempotency_key, request.idempotency_key),
    )
    changed = [name for name, previous, current in comparisons if previous != current]
    return (
        "operation identity was reused with different structured input: "
        + ",".join(changed)
        if changed
        else ""
    )


def _operation_holder_is_live(record: ToolOperationRecord, now: float) -> bool:
    if record.lease_expires_at <= now:
        return False
    if not record.holder_host or record.holder_host != socket.gethostname():
        return True
    if record.holder_pid <= 0:
        return False
    if not _pid_exists(record.holder_pid):
        return False
    expected = str(record.holder_process_start_token or "")
    actual = _read_process_start_token(record.holder_pid)
    if not expected or actual is None:
        return True
    return expected == actual


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _process_start_token() -> str:
    global _PROCESS_START_TOKEN
    if not _PROCESS_START_TOKEN:
        _PROCESS_START_TOKEN = _read_process_start_token(os.getpid()) or ""
    return _PROCESS_START_TOKEN


def _read_process_start_token(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        end = stat.rfind(")")
        fields = stat[end + 1 :].strip().split() if end >= 0 else []
        return fields[19] if len(fields) > 19 else None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None
    return None


def _select_operation(
    conn: Any,
    owner_id: str,
    run_id: str,
    operation_id: str,
) -> ToolOperationRecord | None:
    row = conn.execute(
        """
        SELECT * FROM tool_operations
        WHERE owner_id = ? AND run_id = ? AND operation_id = ?
        """,
        (owner_id, run_id, operation_id),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def _record_from_row(row: Any) -> ToolOperationRecord:
    result = _json_loads(row["result_json"])
    return ToolOperationRecord(
        owner_id=str(row["owner_id"]),
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"] or ""),
        operation_id=str(row["operation_id"]),
        tool=str(row["tool"]),
        args_hash=str(row["args_hash"]),
        idempotency_key=str(row["idempotency_key"]),
        idempotency_scope=str(row["idempotency_scope"]),
        idempotency_namespace=str(row["idempotency_namespace"]),
        status=str(row["status"]),
        holder_id=str(row["holder_id"]),
        holder_host=str(row["holder_host"] or ""),
        holder_pid=int(row["holder_pid"] or 0),
        holder_process_start_token=str(
            row["holder_process_start_token"] or ""
        ),
        generation=int(row["generation"] or 0),
        lease_expires_at=float(row["lease_expires_at"] or 0),
        result=result,
        result_ref=_result_ref(result),
        error_code=str(row["error_code"] or ""),
        unknown_reason=str(row["unknown_reason"] or ""),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        completed_at=float(row["completed_at"] or 0),
    )


def _result_ref(result: dict[str, Any]) -> str:
    direct = str(result.get("result_ref") or "").strip()
    if direct:
        return direct
    envelope = result.get("result_envelope")
    operation = (
        envelope.get("tool_operation")
        if isinstance(envelope, dict)
        else None
    )
    return (
        str(operation.get("result_ref") or "").strip()
        if isinstance(operation, dict)
        else ""
    )


def _json_dumps(value: object) -> str:
    payload = value if isinstance(value, dict) else {}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_loads(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


__all__ = [
    "LocalStoreToolOperationMixin",
    "TOOL_OPERATION_CANCELLED",
    "TOOL_OPERATION_FAILED",
    "TOOL_OPERATION_IDEMPOTENCY_SCOPES",
    "TOOL_OPERATION_RUNNING",
    "TOOL_OPERATION_SUCCEEDED",
    "TOOL_OPERATION_TERMINAL_STATUSES",
    "TOOL_OPERATION_UNKNOWN",
    "ToolOperationClaim",
    "ToolOperationClaimRequest",
    "ToolOperationCompletionRequest",
    "ToolOperationReopenRequest",
    "ToolOperationHolder",
    "ToolOperationOwnershipError",
    "ToolOperationRecord",
    "ToolOperationStateError",
    "new_tool_operation_holder",
]
