
from __future__ import annotations

import json
import time
from typing import Any

from .runtime_gate_models import RuntimeGateLedgerRecord


class LocalStoreRuntimeGateLedgerMixin:

    def record_runtime_gate_ledger(self, record: RuntimeGateLedgerRecord) -> RuntimeGateLedgerRecord:
        now = time.time()
        created_at = record.created_at or now
        updated_at = now
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runtime_gate_ledger(
                    run_id, task_id, operation_id, tool, parameters_json, runtime_gate_json,
                    idempotency_key, args_hash, approval_id, result_ref, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, operation_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    tool=excluded.tool,
                    parameters_json=excluded.parameters_json,
                    runtime_gate_json=excluded.runtime_gate_json,
                    idempotency_key=excluded.idempotency_key,
                    args_hash=excluded.args_hash,
                    approval_id=excluded.approval_id,
                    result_ref=excluded.result_ref,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                _runtime_gate_values(record, created_at, updated_at),
            )
            conn.commit()
        stored = self.get_runtime_gate_ledger(record.run_id, record.operation_id)
        return stored or record

    def get_runtime_gate_ledger(self, run_id: str, operation_id: str) -> RuntimeGateLedgerRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_gate_ledger WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
        return _runtime_gate_record_from_row(row) if row else None

    def list_runtime_gate_ledger(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        tool: str = "",
        limit: int = 100,
    ) -> list[RuntimeGateLedgerRecord]:
        if limit <= 0:
            return []
        where, params = _runtime_gate_query(run_id=run_id, task_id=task_id, tool=tool)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM runtime_gate_ledger
                {where}
                ORDER BY created_at ASC, operation_id ASC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [_runtime_gate_record_from_row(row) for row in rows]

    def runtime_idempotency_ledger(self, *, run_id: str = "", tool: str = "") -> tuple[dict[str, str], ...]:
        records = self.list_runtime_gate_ledger(run_id=run_id, tool=tool)
        rows: list[dict[str, str]] = []
        for record in records:
            if not record.idempotency_key:
                continue
            rows.append({
                "idempotency_key": record.idempotency_key,
                "args_hash": record.args_hash,
                "status": record.status,
                "result_ref": record.result_ref,
            })
        return tuple(rows)


def _runtime_gate_values(record: RuntimeGateLedgerRecord, created_at: float, updated_at: float) -> tuple[object, ...]:
    return (
        record.run_id,
        record.task_id,
        record.operation_id,
        record.tool,
        _json_dumps(record.parameters),
        _json_dumps(record.runtime_gate),
        record.idempotency_key,
        record.args_hash,
        record.approval_id,
        record.result_ref,
        record.status,
        float(created_at),
        float(updated_at),
    )


def _runtime_gate_query(*, run_id: str, task_id: str, tool: str) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for key, value in (("run_id", run_id), ("task_id", task_id), ("tool", tool)):
        if value:
            clauses.append(f"{key} = ?")
            params.append(value)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _runtime_gate_record_from_row(row: Any) -> RuntimeGateLedgerRecord:
    return RuntimeGateLedgerRecord(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        operation_id=str(row["operation_id"]),
        tool=str(row["tool"]),
        parameters=_json_loads(row["parameters_json"]),
        runtime_gate=_json_loads(row["runtime_gate_json"]),
        idempotency_key=str(row["idempotency_key"] or ""),
        args_hash=str(row["args_hash"] or ""),
        approval_id=str(row["approval_id"] or ""),
        result_ref=str(row["result_ref"] or ""),
        status=str(row["status"] or ""),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
    )


def _json_dumps(value: object) -> str:
    payload = value if isinstance(value, dict) else {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_loads(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


__all__ = ["LocalStoreRuntimeGateLedgerMixin"]

