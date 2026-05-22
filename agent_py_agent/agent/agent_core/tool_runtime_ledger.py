# LLM: Tool runtime ledger bridges archive_tool_calls into durable LocalStore rows.
# 模块用途: 从结构化工具归档记录提取 gate、operation、幂等和审批事实并写入本地账本。

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.gates.tool_effects import args_hash_for_call
from ..contracts.tool_protocol_v2 import normalize_tool_call
from ..local_storage import RuntimeGateLedgerRecord


# LLM: persist_tool_runtime_ledger is a best-effort persistence hook after tool execution.
# 函数用途: 将工具入口 runtime_gate 和参数事实写入 LocalStore；缺少 local_store 时静默跳过。
def persist_tool_runtime_ledger(agent: object, archive_record: dict[str, object]) -> None:
    store = getattr(agent, "local_store", None)
    if not hasattr(store, "record_runtime_gate_ledger"):
        return
    record = runtime_gate_ledger_record_from_archive(archive_record)
    if record is None:
        return
    store.record_runtime_gate_ledger(record)


# LLM: write_boundary_with_runtime_ledger injects durable idempotency rows before tool execution.
# 函数用途: 把 LocalStore 中同 run 的幂等账本合并到 write_boundary，供入口 gate 阻断重复副作用。
def write_boundary_with_runtime_ledger(agent: object, params: object) -> dict[str, object] | None:
    boundary = getattr(params, "write_boundary", None)
    store = getattr(agent, "local_store", None)
    if not hasattr(store, "runtime_idempotency_ledger"):
        return boundary
    run_id = _text(getattr(params, "run_id", ""))
    if not run_id:
        return boundary
    persisted = store.runtime_idempotency_ledger(run_id=run_id)
    if not persisted:
        return boundary
    merged = dict(boundary) if isinstance(boundary, dict) else {}
    merged["idempotency_ledger"] = _merged_idempotency_rows(merged.get("idempotency_ledger"), persisted)
    return merged


# LLM: runtime_gate_ledger_record_from_archive converts one archive row into a durable ledger row.
# 函数用途: 只读取 archive_tool_call 里的结构字段，生成 RuntimeGateLedgerRecord。
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


# LLM: _operation_id prefers protocol facts over display call ids.
# 函数用途: 从 operation_id 或 tool_protocol_v2 中读取操作 id，不使用输出文本。
def _operation_id(record: Mapping[str, object]) -> str:
    direct = _text(record.get("operation_id"))
    if direct:
        return direct
    protocol = _dict_value(record.get("tool_protocol_v2"))
    return _text(protocol.get("operation_id"))


# LLM: _idempotency_key reads replay identity from protocol or runtime gate evidence.
# 函数用途: 按结构字段恢复幂等键，供持久化账本和 gate 复用。
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


# LLM: _approval_id reads approval identity from operation facts or gate evidence.
# 函数用途: 持久化已绑定审批号，避免恢复时靠自然语言描述猜审批状态。
def _approval_id(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    direct = _text(record.get("approval_id"))
    if direct:
        return direct
    evidence = _dict_value(runtime_gate.get("evidence"))
    return _text(evidence.get("approval_id"))


# LLM: _args_hash computes the same normalized input hash used by side-effect gates.
# 函数用途: 根据 parameters 机器字段生成参数 hash，缺失时返回空字符串让恢复 gate 自行处理。
def _args_hash(record: Mapping[str, object]) -> str:
    parameters = _dict_value(record.get("parameters"))
    if not parameters:
        return ""
    call = normalize_tool_call(parameters)
    return args_hash_for_call(call.input)


# LLM: _result_ref keeps replay pointed at externalized output or artifact refs.
# 函数用途: 选择最稳定的结果引用，不复制大输出正文。
def _result_ref(record: Mapping[str, object]) -> str:
    for key in ("artifact_ref", "output_path", "source_ref", "path"):
        value = _text(record.get(key))
        if value:
            return value
    return ""


# LLM: _ledger_status maps result and gate booleans to stable replay statuses.
# 函数用途: 将 ok/runtime_gate.allowed 转为 completed/blocked/failed。
def _ledger_status(record: Mapping[str, object], runtime_gate: Mapping[str, object]) -> str:
    if runtime_gate.get("allowed") is not True:
        return "blocked"
    return "completed" if record.get("ok") is True else "failed"


# LLM: _merged_idempotency_rows preserves caller-provided rows and appends persisted rows once.
# 函数用途: 合并 write_boundary 和 LocalStore 幂等账本，避免重复记录膨胀 prompt/ledger。
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


# LLM: _dict_value normalizes optional mapping payloads without accepting prose fallback.
# 函数用途: 将 mapping 转成普通 dict，其他类型返回空对象。
def _dict_value(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# LLM: _text normalizes scalar machine fields for ledger keys.
# 函数用途: 将结构字段转成去空白字符串。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "persist_tool_runtime_ledger",
    "runtime_gate_ledger_record_from_archive",
    "write_boundary_with_runtime_ledger",
]
