# LLM: 本模块读取 owner 私有的原工具输出索引，为 Compact 恢复提供真实 run/attempt/turn/call 身份；引用入口共用 E11a 流式预筛，缺失身份保持未知，不从正文猜测。
# 模块用途: 从任务工作区的工具索引中恢复调用引用、输出引用和可续接的调用事实及来源身份。

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.tool_index_stream import (
    iter_prescreened_tool_index_lines,
    tool_index_prescreen_pattern,
)
from ..common.tool_output_paths import tool_output_index_paths_for_lookup, tool_output_root
from ..tooling.runtime_facts import project_process_runtime_facts
from .tool_output_externalizer import model_visible_tool_parameters

_INTERNAL_LEDGER_TOOLS = {"task_progress"}
# 与 conversation/compact_tool_identity.TOOL_REF_FIELDS 同一四元身份；本层不上引 conversation。
_CALL_IDENTITY_FIELDS = ("run_id", "attempt_id", "turn_id", "call_id")
# 携带记录上的结构化标记：值为 True 表示这条记录属于已经写进会话历史的回合（历史里已有它的原生工具对），
# 续跑只用它重建运行时状态（一次性编排去重、已执行工具、工具轮数），不进本片工具账、模型可见交接或压缩来源。
CARRIED_RUNTIME_ONLY_FIELD = "carried_runtime_only"


# LLM: 旧生命周期续跑也共用scope流式预筛；读全匹配行后才投影，完整四元身份去重，未知身份只合并相同行。
#   归属、重复保留、运行时标记及原读失败合同不变，不将所有历史索引载入列表。
# 函数用途: 从任务原索引恢复调用历史；跨 attempt/turn 的同号调用都保留，旧身份未知时只合并完全相同的索引行。
def carried_tool_call_records(
    workspace: str | Path,
    scope: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = list(_scoped_tool_index_rows(Path(workspace), scope))
    return _carried_records((row, False) for row in rows if _is_indexed_call_fact(row) and _matches_scope(row, scope))


# LLM: 生命周期续跑要读的一个工具索引：只读 root 自己的 blobs/tool_outputs/index.jsonl。label 是结构化来源名
#   （例如 owner_index、task_index），读不到时原样写进不完整事实；runtime_only 表示这些记录只用于运行时状态。
# 类用途: 描述生命周期续跑要读的一个工具索引来源。
@dataclass(frozen=True)
class CarriedIndexSource:
    root: Path
    label: str
    runtime_only: bool = False


# LLM: records 是按请求过滤、去重后的携带记录；unreadable_sources 列出读不到的来源（{"source", "error_type"}），
#   非空就表示 records 不完整，调用方不得把它当成完整的原回合工具事实。
# 类用途: 承载一次携带记录读取的结果，以及是否读全。
@dataclass(frozen=True)
class CarriedToolCallRead:
    records: list[dict[str, Any]] = field(default_factory=list)
    unreadable_sources: tuple[dict[str, str], ...] = ()


# LLM: 生命周期续跑要看到原活动回合的全部工具事实：前台 Gateway 轮写在 owner 根自己的索引，唤醒片写在任务 work
#   索引。每个来源只读它自己的 index.jsonl，不展开到其它 runs；逐行流式读取，先用精确请求编号做字节预筛（只省
#   解析开销），再按结构化 scope 精确匹配，身份去重与 carried_tool_call_records 同一口径，先出现者保留。
#   每个来源独立读取、各自捕获 OSError：一个来源读不到只记进 unreadable_sources，不跳过其它来源；读到一半失败的
#   来源整份丢弃，不留半份。不把整份索引载入内存，只读。
# 函数用途: 从 owner 索引与任务索引里取出属于指定用户回合的工具调用记录，并如实报告哪些来源没读到。
def carried_tool_call_records_for_requests(
    sources: Sequence[CarriedIndexSource],
    request_ids: Sequence[str],
) -> CarriedToolCallRead:
    wanted = tuple(dict.fromkeys(str(item or "").strip() for item in request_ids if str(item or "").strip()))
    if not wanted:
        return CarriedToolCallRead()
    scope = {"conversation_request_id": wanted}
    rows: list[tuple[dict[str, Any], bool]] = []
    unreadable: list[dict[str, str]] = []
    for source in sources:
        try:
            matched = [row for row in _stream_index_rows(Path(source.root), wanted)
                       if _is_indexed_call_fact(row) and _matches_scope(row, scope)]
        except OSError as exc:
            unreadable.append({"source": source.label, "error_type": type(exc).__name__})
            continue
        rows.extend((row, source.runtime_only) for row in matched)
    return CarriedToolCallRead(_carried_records(rows), tuple(unreadable))


# LLM: 输入是已过滤的（索引行, 是否只用于运行时状态）；完整四元身份去重，身份未知时只合并字节相同的行，
#   先出现者保留；只用于运行时状态的记录带 CARRIED_RUNTIME_ONLY_FIELD=True，其它记录不加这个键。纯计算。
# 函数用途: 把索引行转成携带记录并去重，两个读取入口共用同一口径。
def _carried_records(rows: Iterable[tuple[dict[str, Any], bool]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row, runtime_only in rows:
        record = _carried_tool_call_record(row)
        key = _carried_record_key(row, record)
        if key in seen:
            continue
        seen.add(key)
        records.append({**record, CARRIED_RUNTIME_ONLY_FIELD: True} if runtime_only else record)
    return records


# LLM: 四元身份完整时键是去空白后的身份，否则是整行按键排序的 JSON；两类键带不同前缀，互不相撞。纯计算。
# 函数用途: 给一条携带记录算去重键，身份未知的行只和字节完全相同的行合并。
def _carried_record_key(row: dict[str, Any], record: dict[str, Any]) -> tuple[str, ...]:
    values = tuple(record.get(key) for key in _CALL_IDENTITY_FIELDS)
    if all(isinstance(item, str) and item.strip() for item in values):
        return ("identity", *(item.strip() for item in values))
    return ("raw", json.dumps(row, ensure_ascii=False, sort_keys=True))


# LLM: 只读 root 自己的索引文件，二进制逐行读取，记录边界只认物理 LF（与 jsonl_lines 同一规则），行尾一个 CR 去掉；
#   不含任一请求编号字节的行直接跳过，不解析。文件不存在时不产出任何行。
# 函数用途: 流式读出一个索引里可能属于指定请求的行，避免把整份 owner 索引读进内存。
def _stream_index_rows(root: Path, wanted: tuple[str, ...]) -> Iterator[dict[str, Any]]:
    path = tool_output_root(root) / "index.jsonl"
    if not path.exists():
        return
    needles = tuple(item.encode("utf-8") for item in wanted)
    with path.open("rb") as handle:
        yield from filter(None, (_index_line_row(raw, needles) for raw in handle))


# LLM: 字节预筛只省解析开销，不是归属判断；归属仍由调用方按结构化 scope 精确匹配。坏行返回空字典。纯计算。
# 函数用途: 把索引里的一行原始字节转成记录；不含任何请求编号的行直接跳过。
def _index_line_row(raw: bytes, needles: tuple[bytes, ...]) -> dict[str, Any]:
    if not any(needle in raw for needle in needles):
        return {}
    return _json_line(raw.decode("utf-8", errors="replace").removesuffix("\n").removesuffix("\r"))


# LLM: 收尾只读索引元数据；复用 E11a 预筛后仍用原 scope、kind、ledger 排除及投影，错误优先级沿旧整读，不读 artifact。
# 函数用途: 返回当前范围的大输出引用，只解析可能匹配的行，不把全部历史索引装进列表。
def tool_output_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    return _read_scope_refs(Path(workspace), scope, {"tool_outputs": _source_ref})["tool_outputs"]


# LLM: 单类入口不投影其它 kind，保持不相关类型的坏字段不会引发本类投影异常；顺序/重复项不变。
# 函数用途: 返回当前范围的小调用引用，不整读其它回合的索引。
def tool_call_source_refs(workspace: str | Path, scope: dict[str, Any]) -> list[dict[str, Any]]:
    return _read_scope_refs(Path(workspace), scope, {"tool_calls": _tool_call_ref})["tool_calls"]


# LLM: Compact 应用一次遍历同时收两类引用；沿原投影保留身份/双参数/缺失路径/重复项，各类保持原顺序，先调用后输出报投影错。
# 函数用途: 一次枚举和读取索引生成压缩包的小调用与大输出引用，避免原先重复整读两遍。
def tool_source_refs(workspace: str | Path, scope: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return _read_scope_refs(Path(workspace), scope, {"tool_calls": _tool_call_ref, "tool_outputs": _source_ref})


# LLM: 只保留匹配投影与每类首错，不保留原始索引全集；整次读取错误优先于投影错误，投影错按原调用类别顺序抛出。
#   此处没有降级返回：任一错误都仍失败，不返回半份 refs；消费者只在完整扫描结束后拿结果。
# 函数用途: 一次读完当前 scope 引用，延后投影报错以保持旧整读的 IO/UTF-8 优先级。
def _read_scope_refs(
    workspace: Path, scope: dict[str, Any], projectors: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in projectors}
    errors: dict[str, Exception] = {}
    for row in _scoped_tool_index_rows(Path(workspace), scope):
        _collect_scope_projection(row, projectors, result, errors)
    for key in projectors:
        if key in errors:
            raise errors[key]
    return result


# LLM: 单类只投影被请求的类型；投影异常原样保留、全量读取后再抛，不吞错误、不缓存正文；每类只保留首错。
# 函数用途: 将匹配行加入相应的引用桶，或暂记首个字段/投影错误，继续验证后续索引可读性。
def _collect_scope_projection(
    row: dict[str, Any], projectors: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    result: dict[str, list[dict[str, Any]]], errors: dict[str, Exception],
) -> None:
    kind = "tool_calls" if _is_tool_call_row(row) else "tool_outputs" if _is_tool_output_row(row) else ""
    projector = projectors.get(kind)
    if projector is None or kind in errors:
        return
    try:
        result[kind].append(projector(row))
    except Exception as exc:
        errors[kind] = exc


# LLM: 不根据目录名推断归属，沿原 owner/runs/tasks 路径一次枚举；预筛只选一个非空 AND 条件作为必要条件。
#   最终匹配仍用本模块原顶层四键与旧 conversation_request_id 回退，不借用 Curator 的嵌套 scope 规则。
# 函数用途: 流式产出当前范围的索引行；IO/UTF-8 错误向上抛出，坏 JSON/非对象仍忽略。
def _scoped_tool_index_rows(workspace: Path, scope: dict[str, Any]) -> Iterator[dict[str, Any]]:
    prescreen = _scope_prescreen_pattern(scope)
    for path in tool_output_index_paths_for_lookup(workspace):
        yield from (
            row for _, text in iter_prescreened_tool_index_lines(path, prescreen, universal_newlines=True)
            if (row := _json_line(text)) and _matches_scope(row, scope)
        )


# LLM: scope 是各键 AND、每键允许值 OR；任意一个安全非空键足以构成必要条件。旧请求字段回退的同值也必在原字节中。
#   任一不安全值则该键不能用于预筛；所有键都不可证明或没有约束时 None=逐行解析，不能静默漏引用。
# 函数用途: 复用 E11a 的字面量/转义安全判定，为一个范围选取可证明不漏的预筛正则。
def _scope_prescreen_pattern(scope: dict[str, Any]) -> re.Pattern[bytes] | None:
    for key in ("conversation_request_id", "request_id", "run_id", "task_id"):
        pattern = tool_index_prescreen_pattern(_scope_expected_values(scope.get(key)))
        if pattern is not None:
            return pattern
    return None


# LLM: 与原 _scope_value_matches 的 str(value or '')/strip 和四种集合类型完全同源，不改变空允许集=不筛选语义。
# 函数用途: 将范围的一项或一组值投影成原匹配使用的非空字符串，供匹配和预筛共同使用。
def _scope_expected_values(expected: object) -> tuple[str, ...]:
    values = expected if isinstance(expected, (list, tuple, set, frozenset)) else (expected,)
    return tuple(text for item in values if (text := str(item or "").strip()))


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


def _matches_scope(row: dict[str, Any], scope: dict[str, Any]) -> bool:
    return all(
        not expected or _scope_value_matches(row, key, expected)
        for key in ("conversation_request_id", "request_id", "run_id", "task_id")
        if (expected := scope.get(key))
    )


# LLM: Active-turn recovery may receive a bounded batch of child wakes. Match each row by
# typed conversation request ids while accepting pre-field legacy rows only when request_id
# already equals that exact id; never broaden to the durable task id implicitly.
# 值规范化与预筛共用，空值仍不约束；归属依旧只由结构化字段决定。
# 函数用途: 判断工具索引行是否属于一个或一组明确用户回合，并兼容字段落盘前的同值旧记录。
def _scope_value_matches(row: dict[str, Any], key: str, expected: object) -> bool:
    allowed = set(_scope_expected_values(expected))
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
# both are indexed calls; exact identity validation and deduplication happen in carried_tool_call_records.
# 函数用途: 判断索引行能否作为一次历史工具调用恢复。
def _is_indexed_call_fact(row: dict[str, Any]) -> bool:
    return (
        str(row.get("kind") or "") in {"tool_call", "tool_output"}
        and bool(str(row.get("tool") or "").strip())
        and bool(str(row.get("call_id") or "").strip())
    )


# LLM: Convert index metadata into the carried archive contract. Successful indexed calls prove the
# handler ran; failed legacy rows remain fail-closed. Copy run/attempt/turn exactly from the original
# row without coercion or recovery-request fallback; artifact paths stay refs, process facts stay bounded.
# 函数用途: 恢复原始调用身份、执行参数、结果引用与有界进程事实；缺字段或非字符串身份保持未知，不猜来源或清理成功。
def _carried_tool_call_record(row: dict[str, Any]) -> dict[str, Any]:
    path = str(row.get("path") or "").strip()
    digest = str(row.get("sha256") or "").strip()
    execution = row.get("tool_execution")
    execution = execution if isinstance(execution, dict) else {}
    handler_executed = execution.get("handler_executed")
    if not isinstance(handler_executed, bool):
        handler_executed = row.get("ok") is True
    record: dict[str, Any] = {
        "id": row.get("call_id", ""),
        "call_id": row.get("call_id", ""),
        "scoped_call_id": str(row.get("scoped_call_id") or ""),
        "request_id": str(row.get("request_id") or ""),
        "conversation_request_id": str(row.get("conversation_request_id") or ""),
        "run_id": row.get("run_id", ""),
        "attempt_id": row.get("attempt_id", ""),
        "turn_id": row.get("turn_id", ""),
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
    if process := project_process_runtime_facts(row.get("tool_process")):
        record["tool_result_envelope"] = {"process": process}
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
    "CarriedIndexSource",
    "CarriedToolCallRead",
    "carried_tool_call_records",
    "carried_tool_call_records_for_requests",
    "tool_call_refs",
    "tool_call_source_refs",
    "tool_output_artifact_refs",
    "tool_output_source_refs",
    "tool_source_refs",
]
