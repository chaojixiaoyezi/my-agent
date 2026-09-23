# LLM: 本模块读取 owner 私有的原工具输出索引，为 Compact 恢复提供真实 run/attempt/turn/call 身份；缺失身份保持未知，不从正文猜测。
# 模块用途: 从任务工作区的工具索引中恢复调用引用、输出引用和可续接的调用事实及来源身份。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..common.json_io import jsonl_lines
from ..common.tool_output_paths import tool_output_index_paths_for_lookup
from .tool_output_externalizer import model_visible_tool_parameters

_INTERNAL_LEDGER_TOOLS = {"task_progress"}


# LLM: Child lifecycle wakes share the originating conversation turn. Deduplicate complete
# run/attempt/turn/call identities; unknown legacy identities collapse only for identical index rows.
# 函数用途: 从任务原索引恢复调用历史；旧身份未知时只合并完全相同的索引行。
def carried_tool_call_records(
    workspace: str | Path,
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    seen_unknown_rows: set[str] = set()
    for row in rows:
        if not _is_indexed_call_fact(row) or not _matches_scope(row, scope):
            continue
        record = _carried_tool_call_record(row)
        identity = tuple(
            str(record.get(key) or "").strip()
            for key in ("run_id", "attempt_id", "turn_id", "call_id")
        )
        if all(identity):
            if identity in seen:
                continue
            seen.add(identity)
        else:
            raw_row = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if raw_row in seen_unknown_rows:
                continue
            seen_unknown_rows.add(raw_row)
        records.append(record)
    return records


def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    # 大输出恢复产物经 _write_output_artifact→_append_index 已写 kind=tool_output
    # 行(带 path)进 index.jsonl, 直接读 index 即可, 无需另扫 artifact 文件。
    rows = _read_tool_output_index(Path(workspace))
    return [_source_ref(row) for row in rows if _is_tool_output_row(row) and _matches_scope(row, scope)]


def tool_call_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read_tool_output_index(Path(workspace))
    return [_tool_call_ref(row) for row in rows if _is_tool_call_row(row) and _matches_scope(row, scope)]


# LLM: Restore refs retain the indexed call identity, execution parameters and model_parameters;
# neither parameter view nor missing identity may be silently overwritten.
# 函数用途: 从 Compact 恢复包提取外置工具输出引用、真实调用身份及两套参数视图。
def tool_output_artifact_refs(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    items = source_refs.get("tool_outputs", []) if isinstance(source_refs.get("tool_outputs"), list) else []
    return [
        {
            "kind": "tool_output",
            "path": str(item.get("path", "") or ""),
            "tool": str(item.get("tool", "") or ""),
            "call_id": str(item.get("call_id", "") or ""),
            "scoped_call_id": str(item.get("scoped_call_id", "") or ""),
            "run_id": str(item.get("run_id") or ""),
            "attempt_id": str(item.get("attempt_id") or ""),
            "turn_id": str(item.get("turn_id") or ""),
            "source_path": str(item.get("source_input") or ""),
            "parameters": dict(item.get("parameters", {}) if isinstance(item.get("parameters"), dict) else {}),
            "model_parameters": model_visible_tool_parameters(item),
            "ok": item.get("ok"),
            "status": str(item.get("status") or ""),
            "error_code": str(item.get("error_code") or ""),
            "sha256": str(item.get("sha256", "") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "read_window": dict(item.get("read_window", {}) if isinstance(item.get("read_window"), dict) else {}),
            "page_window": dict(item.get("page_window", {}) if isinstance(item.get("page_window"), dict) else {}),
        }
        for item in items
        if item.get("path") and _is_model_visible_tool_output(item)
    ]


def tool_call_refs(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs.get("source_refs"), dict) else {}
    items = source_refs.get("tool_calls", []) if isinstance(source_refs.get("tool_calls"), list) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def _read_tool_output_index(workspace: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in tool_output_index_paths_for_lookup(workspace):
        if not path.exists():
            continue
        rows.extend(_read_tool_output_index_path(path))
    return rows


def _read_tool_output_index_path(path: Path) -> list[dict[str, Any]]:
    return [
        payload
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        for line in jsonl_lines(path.read_text(encoding="utf-8"))
        if (payload := _json_line(line))
    ]


def _matches_scope(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    return all(
        not expected or _scope_value_matches(row, key, expected)
        for key in ("conversation_request_id", "request_id", "run_id", "task_id")
        if (expected := scope.get(key))
    )


# LLM: Active-turn recovery may receive a bounded batch of child wakes. Match each row by
# typed conversation request ids while accepting pre-field legacy rows only when request_id
# already equals that exact id; never broaden to the durable task id implicitly.
# 函数用途: 判断工具索引行是否属于一个或一组明确用户回合，并兼容字段落盘前的同值旧记录。
def _scope_value_matches(row: dict[str, Any], key: str, expected: object) -> bool:
    values = (
        tuple(str(item or "").strip() for item in expected)
        if isinstance(expected, (list, tuple, set, frozenset))
        else (str(expected or "").strip(),)
    )
    allowed = {item for item in values if item}
    if not allowed:
        return True
    actual = str(row.get(key) or "").strip()
    if actual in allowed:
        return True
    return (
        key == "conversation_request_id"
        and not actual
        and str(row.get("request_id") or "").strip() in allowed
    )


def _is_tool_output_row(row: dict[str, Any]) -> bool:
    return (
        str(row.get("kind") or "") == "tool_output"
        and bool(str(row.get("path") or "").strip())
        and _is_model_visible_tool_output(row)
    )


def _is_model_visible_tool_output(row: dict[str, Any]) -> bool:
    return str(row.get("tool") or "").strip() not in _INTERNAL_LEDGER_TOOLS


def _is_tool_call_row(row: dict[str, Any]) -> bool:
    return str(row.get("kind") or "") == "tool_call"


# LLM: A small output is indexed as tool_call and an externalized output as tool_output;
# both are one exact invocation, distinguished by scoped_call_id rather than filename.
# 函数用途: 判断索引行能否作为一次历史工具调用恢复。
def _is_indexed_call_fact(row: dict[str, Any]) -> bool:
    return (
        str(row.get("kind") or "") in {"tool_call", "tool_output"}
        and bool(str(row.get("tool") or "").strip())
        and bool(str(row.get("call_id") or "").strip())
    )


# LLM: Convert index metadata into the carried archive contract without inventing absent
# attempt/turn ids; failed legacy rows remain fail-closed. Artifact paths stay refs.
# 函数用途: 把工具索引转换成保留真实调用身份、参数和结果引用的轻量记录。
def _carried_tool_call_record(row: dict[str, Any]) -> dict[str, Any]:
    path = str(row.get("path") or "").strip()
    digest = str(row.get("sha256") or "").strip()
    execution = row.get("tool_execution")
    execution = execution if isinstance(execution, dict) else {}
    handler_executed = execution.get("handler_executed")
    if not isinstance(handler_executed, bool):
        handler_executed = row.get("ok") is True
    record: dict[str, Any] = {
        "id": str(row.get("call_id") or ""),
        "call_id": str(row.get("call_id") or ""),
        "scoped_call_id": str(row.get("scoped_call_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "conversation_request_id": str(row.get("conversation_request_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "attempt_id": str(row.get("attempt_id") or ""),
        "turn_id": str(row.get("turn_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "tool": str(row.get("tool") or ""),
        "parameters": dict(row.get("parameters") or {})
        if isinstance(row.get("parameters"), dict)
        else {},
        "model_parameters": model_visible_tool_parameters(row),
        "ok": row.get("ok") is True,
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "reported_error_code": str(row.get("reported_error_code") or ""),
        "handler_executed": handler_executed,
        "duration_ms": int(execution.get("duration_ms") or 0),
        "output_externalized": bool(row.get("output_externalized") or path),
        "output_size_bytes": int(row.get("size_bytes") or 0),
        "tool_output_trust": str(row.get("tool_output_trust") or "runtime"),
        "tool_output_redaction": str(row.get("tool_output_redaction") or "default"),
    }
    if digest:
        record["output_hash"] = digest
    failure_stage = str(execution.get("failure_stage") or "").strip()
    if failure_stage:
        record["failure_stage"] = failure_stage
    if path:
        record["output_path"] = path
        record["artifact_ref"] = path
    for key in ("read_window", "page_window"):
        value = row.get(key)
        if isinstance(value, dict):
            record[key] = dict(value)
    _attach_carried_operation_facts(record, row.get("tool_operation"))
    return record


# LLM: Carried operation facts come only from the externalizer's typed whitelist; ``ok`` or
# output prose can never synthesize a succeeded mutation across background slices.
# 函数用途: 把耐久索引里的副作用操作身份和终态恢复成现有工具核验记录字段。
def _attach_carried_operation_facts(
    record: dict[str, Any],
    value: object,
) -> None:
    if not isinstance(value, dict):
        return
    for source_key, target_key in (
        ("operation_id", "operation_id"),
        ("result_ref", "result_ref"),
        ("status", "tool_operation_status"),
        ("action", "tool_operation_action"),
        ("idempotency_scope", "tool_operation_idempotency_scope"),
        (
            "reconciliation_source_ref",
            "tool_operation_reconciliation_source_ref",
        ),
    ):
        text = str(value.get(source_key) or "").strip()
        if text:
            record[target_key] = text
    if isinstance(value.get("replayed"), bool):
        record["tool_operation_replayed"] = value.get("replayed") is True


# LLM: A source ref carries the indexed call's exact identity and both parameter views; absent
# attempt/turn ids remain empty rather than inferred from the current Compact request.
# 函数用途: 把工具输出索引行转换成保留真实调用身份的 Compact 来源引用。
def _source_ref(row: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(row.get("path") or ""))
    return {
        "kind": "tool_output",
        "path": str(path),
        "artifact_ref": str(path),
        "exists": path.exists(),
        "tool": str(row.get("tool", "") or ""),
        "call_id": str(row.get("call_id", "") or ""),
        "scoped_call_id": str(row.get("scoped_call_id", "") or ""),
        "source_input": str(row.get("source_input") or ""),
        "source_path": str(row.get("source_input") or ""),
        "parameters": dict(row.get("parameters", {}) if isinstance(row.get("parameters"), dict) else {}),
        "model_parameters": model_visible_tool_parameters(row),
        "request_id": str(row.get("request_id", "") or ""),
        "conversation_request_id": str(row.get("conversation_request_id", "") or ""),
        "run_id": str(row.get("run_id", "") or ""),
        "attempt_id": str(row.get("attempt_id") or ""),
        "turn_id": str(row.get("turn_id") or ""),
        "task_id": str(row.get("task_id", "") or ""),
        "ok": row.get("ok"),
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "read_window": dict(row.get("read_window", {}) if isinstance(row.get("read_window"), dict) else {}),
        "page_window": dict(row.get("page_window", {}) if isinstance(row.get("page_window"), dict) else {}),
    }


# LLM: Small-call refs preserve the same exact call identity and dual-view contract as externalized
# outputs; missing legacy attempt/turn ids stay unknown.
# 函数用途: 把未外置正文的工具调用索引行转换成保留真实身份的 Compact 引用。
def _tool_call_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "tool_call",
        "tool": str(row.get("tool", "") or ""),
        "call_id": str(row.get("call_id", "") or ""),
        "scoped_call_id": str(row.get("scoped_call_id", "") or ""),
        "source_input": str(row.get("source_input") or ""),
        "source_path": str(row.get("source_input") or ""),
        "parameters": dict(row.get("parameters", {}) if isinstance(row.get("parameters"), dict) else {}),
        "model_parameters": model_visible_tool_parameters(row),
        "request_id": str(row.get("request_id", "") or ""),
        "conversation_request_id": str(row.get("conversation_request_id", "") or ""),
        "run_id": str(row.get("run_id", "") or ""),
        "attempt_id": str(row.get("attempt_id") or ""),
        "turn_id": str(row.get("turn_id") or ""),
        "task_id": str(row.get("task_id", "") or ""),
        "ok": row.get("ok"),
        "status": str(row.get("status") or ""),
        "error_code": str(row.get("error_code") or ""),
        "sha256": str(row.get("sha256", "") or ""),
        "size_bytes": int(row.get("size_bytes", 0) or 0),
        "output_externalized": bool(row.get("output_externalized")),
        "read_window": dict(row.get("read_window", {}) if isinstance(row.get("read_window"), dict) else {}),
        "page_window": dict(row.get("page_window", {}) if isinstance(row.get("page_window"), dict) else {}),
    }


def _json_line(line: str) -> dict[str, Any]:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "carried_tool_call_records",
    "tool_call_refs",
    "tool_call_source_refs",
    "tool_output_artifact_refs",
    "tool_output_source_refs",
]
