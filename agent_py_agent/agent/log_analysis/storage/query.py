# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Controlled local query engine for log-analysis events."""

import time
from dataclasses import dataclass
from typing import Any

from ..cases.evidence import LocalEvidenceStore, QueryEvidencePayload
from .base import (
    DEFAULT_PREVIEW_LIMIT,
    QueryCriteria,
    QueryRecord,
    QueryResult,
    dict_to_model,
    event_time_value,
    evidence_path_from_ref,
    model_to_dict,
    normalize_limit,
    stable_digest,
    utc_now,
)
from .local_store import LocalLogStore
from .query_projection import (
    query_matches,
    sanitize_event_for_preview,
    summarize_rows,
)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _QuerySummaryInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _QuerySummaryInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _QuerySummaryInput:
    rows: list[dict[str, Any]]
    parameters: dict[str, Any]
    returned_row_count: int
    truncated: bool
    event_read_audit: dict[str, Any] | None


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _QueryResultInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _QueryResultInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _QueryResultInput:
    query_id: str
    parameters: dict[str, Any]
    row_count: int
    truncated: bool
    evidence_path: str
    evidence: Any
    duration_ms: int
    summary: dict[str, Any]
    limited_rows: list[dict[str, Any]]
    preview_limit: int


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _WriteQueryEvidenceInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _WriteQueryEvidenceInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _WriteQueryEvidenceInput:
    # LLM: Query evidence writes share this small bundle with LocalEvidenceStore.
    query_id: str
    parameters: dict[str, Any]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    summary: dict[str, Any]


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 SecurityQueryOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityQueryOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SecurityQueryOptions:
    require_time_range: bool = True
    preview_limit: int = DEFAULT_PREVIEW_LIMIT
    max_limit: int | None = None


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 execute_security_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 execute security query 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def execute_security_query(
    store: LocalLogStore,
    criteria: QueryCriteria | dict[str, Any],
    *,
    options: SecurityQueryOptions | None = None,
    require_time_range: bool = True,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
    max_limit: int | None = None,
) -> QueryResult:
    query_options = options or SecurityQueryOptions(
        require_time_range=bool(require_time_range),
        preview_limit=int(preview_limit),
        max_limit=max_limit,
    )
    query = _criteria(criteria)
    if query_options.require_time_range and (not query.start_time or not query.end_time):
        raise ValueError("start_time and end_time are required for controlled security queries")

    result_payload = _execute_query_payload(store, query, query_options)
    _save_query_record(store, result_payload)
    return _query_result(_QueryResultInput(preview_limit=query_options.preview_limit, **result_payload))


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _execute_query_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 execute query payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _execute_query_payload(
    store: LocalLogStore,
    query: QueryCriteria,
    query_options: SecurityQueryOptions,
) -> dict[str, Any]:
    start = time.perf_counter()
    limit = normalize_limit(query.limit, max_limit=query_options.max_limit)
    parameters = _criteria_to_parameters(query, limit)
    rows = _matching_rows(store, query)
    event_read_audit = store.last_read_audit(store.events_path)
    row_count = len(rows)
    truncated = row_count > limit
    limited_rows = rows[:limit]
    summary = _query_summary(_QuerySummaryInput(rows, parameters, len(limited_rows), truncated, event_read_audit))
    query_id = _query_id(parameters)
    evidence = _write_query_evidence(
        store,
        _WriteQueryEvidenceInput(
            query_id=query_id,
            parameters=parameters,
            rows=limited_rows,
            row_count=row_count,
            truncated=truncated,
            summary=summary,
        ),
    )
    store.upsert_evidence_ref(evidence)
    duration_ms = int((time.perf_counter() - start) * 1000)
    evidence_path = evidence_path_from_ref(evidence)
    result_payload = dict(
        query_id=query_id,
        parameters=parameters,
        row_count=row_count,
        truncated=truncated,
        evidence_path=evidence_path,
        evidence=evidence,
        duration_ms=duration_ms,
        summary=summary,
        limited_rows=limited_rows,
    )
    return result_payload


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _save_query_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 save query record 相关记录，集中处理目标路径、格式化和状态更新。
def _save_query_record(
    store: LocalLogStore,
    payload: dict[str, Any],
) -> None:
    store.save_query_record(
        QueryRecord(
            query_id=payload["query_id"],
            parameters=payload["parameters"],
            row_count=payload["row_count"],
            truncated=payload["truncated"],
            evidence_path=payload["evidence_path"],
            duration_ms=payload["duration_ms"],
            created_at=utc_now(),
            summary=payload["summary"],
        )
    )


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _matching_rows 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matching rows 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _matching_rows(store: LocalLogStore, query: QueryCriteria) -> list[dict[str, Any]]:
    rows = [row for row in store.list_events() if query_matches(row, query)]
    rows.sort(key=lambda row: str(event_time_value(row) or ""))
    return rows


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _query_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query summary 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_summary(data: _QuerySummaryInput) -> dict[str, Any]:
    summary = summarize_rows(data.rows, data.parameters)
    summary["returned_row_count"] = data.returned_row_count
    summary["truncated"] = data.truncated
    if data.event_read_audit:
        summary["storage_read_audit"] = data.event_read_audit
        summary["skipped_storage_lines"] = data.event_read_audit.get("skipped_lines", 0)
        summary["corrupt_storage_lines"] = data.event_read_audit.get("corrupt_lines", 0)
    return summary


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _write_query_evidence 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write query evidence 相关记录，集中处理目标路径、格式化和状态更新。
def _write_query_evidence(
    store: LocalLogStore,
    data: _WriteQueryEvidenceInput,
):
    return LocalEvidenceStore(store.root).write_query_result(
        payload=QueryEvidencePayload(
            query_id=data.query_id,
            parameters=data.parameters,
            rows=data.rows,
            row_count=data.row_count,
            truncated=data.truncated,
            summary=data.summary,
        )
    )


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _query_result 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query result 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_result(payload: _QueryResultInput) -> QueryResult:
    limited_rows = payload.limited_rows
    return QueryResult(
        query_id=payload.query_id,
        parameters=payload.parameters,
        row_count=payload.row_count,
        truncated=payload.truncated,
        evidence_path=payload.evidence_path,
        evidence_ref=payload.evidence,
        duration_ms=payload.duration_ms,
        summary=payload.summary,
        rows=limited_rows,
        preview_rows=[sanitize_event_for_preview(row) for row in limited_rows[: payload.preview_limit]],
    )


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _criteria 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 criteria 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def _criteria(criteria: QueryCriteria | dict[str, Any]) -> QueryCriteria:
    if isinstance(criteria, QueryCriteria):
        return criteria
    return dict_to_model(QueryCriteria, criteria)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _criteria_to_parameters 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 criteria to parameters 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def _criteria_to_parameters(criteria: QueryCriteria, limit: int) -> dict[str, Any]:
    data = model_to_dict(criteria)
    data["limit"] = limit
    return data


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _query_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query id 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_id(parameters: dict[str, Any]) -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace("Z", "")
    payload = {"parameters": parameters, "nonce": time.time_ns()}
    return f"query-{stamp}-{stable_digest(payload)[:10]}"
