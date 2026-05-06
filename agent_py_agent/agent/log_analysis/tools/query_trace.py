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


def trace_case_params(kwargs: dict[str, Any]) -> TraceCaseParams:
    return TraceCaseParams(
        store=kwargs.get("store"),
        root=kwargs.get("root"),
        start_time=kwargs.get("start_time"),
        end_time=kwargs.get("end_time"),
        limit=kwargs.get("limit", DEFAULT_QUERY_LIMIT),
        max_limit=kwargs.get("max_limit"),
        max_queries=int(kwargs.get("max_queries", MAX_TRACE_CASE_QUERIES)),
    )


def trace_case_queries(
    local_store: LocalLogStore,
    seeds: dict[str, list[str]],
    params: TraceCaseParams,
    query_one: TraceQuery,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        append_trace_field_queries(local_store, queries, field, seeds.get(field, []), params, query_one)
        if len(queries) >= params.max_queries:
            break
    return queries


def append_trace_field_queries(
    local_store: LocalLogStore,
    queries: list[dict[str, Any]],
    field: str,
    values: list[str],
    params: TraceCaseParams,
    query_one: TraceQuery,
) -> None:
    for value in values:
        if len(queries) >= params.max_queries:
            break
        queries.append(query_one(local_store, field, value, params))


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
