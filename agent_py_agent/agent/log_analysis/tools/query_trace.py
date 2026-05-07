from __future__ import annotations

"""Trace-case helpers for security log query tools."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import DEFAULT_QUERY_LIMIT, LocalLogStore

MAX_TRACE_CASE_QUERIES = 20
TraceQuery = Callable[["TraceFieldQueryRequest"], dict[str, Any]]


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


@dataclass(frozen=True)
class TraceCaseParamLimits:
    # LLM: Trace limits are bundled to keep tool calls explicit and bounded.
    limit: int | None = DEFAULT_QUERY_LIMIT
    max_limit: int | None = None
    max_queries: int = MAX_TRACE_CASE_QUERIES


@dataclass(frozen=True)
class TraceCaseQueryRequest:
    # LLM: Case tracing passes query state as one bundle to avoid drifting helper signatures.
    local_store: LocalLogStore
    seeds: dict[str, list[str]]
    params: TraceCaseParams
    query_one: TraceQuery


@dataclass(frozen=True)
class TraceFieldQueryRequest:
    local_store: LocalLogStore
    field: str
    value: str
    params: TraceCaseParams


@dataclass(frozen=True)
class _TraceFieldQueryInput:
    queries: list[dict[str, Any]]
    field: str
    values: list[str]
    request: TraceCaseQueryRequest


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


def trace_case_queries(request: TraceCaseQueryRequest) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        values = request.seeds.get(field, [])
        append_trace_field_queries(_TraceFieldQueryInput(queries, field, values, request))
        if len(queries) >= request.params.max_queries:
            break
    return queries


def append_trace_field_queries(request: _TraceFieldQueryInput) -> None:
    params = request.request.params
    for value in request.values:
        if len(request.queries) >= params.max_queries:
            break
        field_request = TraceFieldQueryRequest(request.request.local_store, request.field, value, params)
        request.queries.append(request.request.query_one(field_request))


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
