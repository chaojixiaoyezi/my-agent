from __future__ import annotations

"""Trace-case helpers for security log query tools."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import DEFAULT_QUERY_LIMIT, LocalLogStore

MAX_TRACE_CASE_QUERIES = 20
TraceQuery = Callable[[LocalLogStore, str, str, "TraceCaseParams"], dict[str, Any]]


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
class _TraceFieldQueryInput:
    local_store: LocalLogStore
    queries: list[dict[str, Any]]
    field: str
    values: list[str]
    params: TraceCaseParams
    query_one: TraceQuery


def trace_case_params(
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> TraceCaseParams:
    return TraceCaseParams(
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
        max_queries=int(max_queries),
    )


def trace_case_queries(
    local_store: LocalLogStore,
    seeds: dict[str, list[str]],
    params: TraceCaseParams,
    query_one: TraceQuery,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        append_trace_field_queries(_TraceFieldQueryInput(local_store, queries, field, seeds.get(field, []), params, query_one))
        if len(queries) >= params.max_queries:
            break
    return queries


def append_trace_field_queries(data: _TraceFieldQueryInput | LocalLogStore, *args: Any) -> None:
    if not isinstance(data, _TraceFieldQueryInput):
        data = _trace_field_query_input(data, args)
    for value in data.values:
        if len(data.queries) >= data.params.max_queries:
            break
        data.queries.append(data.query_one(data.local_store, data.field, value, data.params))


def _trace_field_query_input(local_store: LocalLogStore, args: tuple[Any, ...]) -> _TraceFieldQueryInput:
    queries, field, values, params, query_one = args
    return _TraceFieldQueryInput(local_store, queries, field, values, params, query_one)


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
