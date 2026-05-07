# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Trace-case helpers for security log query tools."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import DEFAULT_QUERY_LIMIT, LocalLogStore

MAX_TRACE_CASE_QUERIES = 20
TraceQuery = Callable[["TraceFieldQueryRequest"], dict[str, Any]]


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 TraceCaseParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TraceCaseParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TraceCaseParams:
    """Parameter bundle for trace_case."""

    store: LocalLogStore | None = None
    root: str | Path | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int | None = DEFAULT_QUERY_LIMIT
    max_limit: int | None = None
    max_queries: int = MAX_TRACE_CASE_QUERIES


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 TraceCaseParamLimits 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TraceCaseParamLimits 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TraceCaseParamLimits:
    # LLM: Trace limits are bundled to keep tool calls explicit and bounded.
    limit: int | None = DEFAULT_QUERY_LIMIT
    max_limit: int | None = None
    max_queries: int = MAX_TRACE_CASE_QUERIES


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 TraceCaseQueryRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TraceCaseQueryRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TraceCaseQueryRequest:
    # LLM: Case tracing passes query state as one bundle to avoid drifting helper signatures.
    local_store: LocalLogStore
    seeds: dict[str, list[str]]
    params: TraceCaseParams
    query_one: TraceQuery


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 TraceFieldQueryRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TraceFieldQueryRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TraceFieldQueryRequest:
    local_store: LocalLogStore
    field: str
    value: str
    params: TraceCaseParams


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _TraceFieldQueryInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _TraceFieldQueryInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _TraceFieldQueryInput:
    queries: list[dict[str, Any]]
    field: str
    values: list[str]
    request: TraceCaseQueryRequest


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 trace_case_params 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace case params 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def trace_case_params(
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    params: TraceCaseParamLimits | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> TraceCaseParams:
    query_limits = params or TraceCaseParamLimits(
        limit=limit,
        max_limit=max_limit,
        max_queries=int(max_queries),
    )
    return TraceCaseParams(
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=query_limits.limit,
        max_limit=query_limits.max_limit,
        max_queries=int(query_limits.max_queries),
    )


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 trace_case_queries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace case queries 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def trace_case_queries(request: TraceCaseQueryRequest) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        values = request.seeds.get(field, [])
        append_trace_field_queries(_TraceFieldQueryInput(queries, field, values, request))
        if len(queries) >= request.params.max_queries:
            break
    return queries


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 append_trace_field_queries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append trace field queries 相关记录，集中处理目标路径、格式化和状态更新。
def append_trace_field_queries(request: _TraceFieldQueryInput) -> None:
    params = request.request.params
    for value in request.values:
        if len(request.queries) >= params.max_queries:
            break
        field_request = TraceFieldQueryRequest(request.request.local_store, request.field, value, params)
        request.queries.append(request.request.query_one(field_request))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 trace_case_response 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace case response 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def trace_case_response(case_id: str, queries: list[dict[str, Any]], max_queries: int) -> dict[str, Any]:
    return {
        "tool": "security_trace_case",
        "case_id": case_id,
        "query_count": len(queries),
        "truncated": len(queries) >= max_queries,
        "row_count": sum(int(query["row_count"]) for query in queries),
        "queries": queries,
        "evidence_refs": [ref for query in queries for ref in query.get("evidence_refs", [])],
    }
