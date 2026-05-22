# LLM: Runtime gate ledger persists mandatory tool-entry facts for replay and recovery.
# 模块用途: 写入和查询 runtime gate、approval、idempotency 和参数 hash 账本。

from __future__ import annotations

import json
import time
from typing import Any

from .runtime_gate_models import RuntimeGateLedgerRecord


# LLM: LocalStoreRuntimeGateLedgerMixin owns durable tool-gate projection APIs.
# 类用途: 为 LocalStore 增加 runtime gate 账本读写能力。
class LocalStoreRuntimeGateLedgerMixin:

    # LLM: record_runtime_gate_ledger upserts one operation-scoped runtime gate row.
    # 函数用途: 以 run_id + operation_id 作为幂等键写入工具入口 gate 事实。
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

    # LLM: get_runtime_gate_ledger reads one operation-scoped gate row.
    # 函数用途: 按 run_id + operation_id 读取单条工具入口 gate 账本。
    def get_runtime_gate_ledger(self, run_id: str, operation_id: str) -> RuntimeGateLedgerRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_gate_ledger WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
        return _runtime_gate_record_from_row(row) if row else None

    # LLM: list_runtime_gate_ledger returns stable replay rows for a run/task/tool scope.
    # 函数用途: 查询一组工具入口 gate 账本，供 resume/replay/审计按结构字段读取。
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

    # LLM: runtime_idempotency_ledger projects persisted rows into IdempotencyLedgerGate input.
    # 函数用途: 输出 idempotency_key、args_hash、status、result_ref，供幂等 gate 直接消费。
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


# LLM: _runtime_gate_values serializes one record using stable JSON fields.
# 函数用途: 生成 runtime_gate_ledger upsert 参数，确保 JSON 排序稳定。
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


# LLM: _runtime_gate_query keeps list filters structural and deterministic.
# 函数用途: 根据 run/task/tool 机器字段构造 where 片段，不读取自然语言正文。
def _runtime_gate_query(*, run_id: str, task_id: str, tool: str) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for key, value in (("run_id", run_id), ("task_id", task_id), ("tool", tool)):
        if value:
            clauses.append(f"{key} = ?")
            params.append(value)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


# LLM: _runtime_gate_record_from_row hydrates one SQLite row into the public dataclass.
# 函数用途: 读取 JSON 字段并保持缺失或损坏值为安全空对象。
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


# LLM: _json_dumps gives ledger records stable JSON encoding.
# 函数用途: 序列化 dict 字段，非 dict 值降级为空对象。
def _json_dumps(value: object) -> str:
    payload = value if isinstance(value, dict) else {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# LLM: _json_loads protects replay from malformed legacy JSON rows.
# 函数用途: 反序列化账本 JSON 字段，失败时返回空对象让上层 gate 进入恢复。
def _json_loads(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


__all__ = ["LocalStoreRuntimeGateLedgerMixin"]

