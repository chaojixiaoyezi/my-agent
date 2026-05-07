"""LLM: Security log query functions used by log-analysis tools.

Trace-case query assembly lives in query_trace.py; this file is the public facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import (
    DEFAULT_QUERY_LIMIT,
    LocalLogStore,
    QueryCriteria,
    QueryResult,
    execute_security_query,
)
from .query_trace import (
    MAX_TRACE_CASE_QUERIES,
    TraceCaseParams,
    trace_case_params,
    trace_case_queries,
    trace_case_response,
)


@dataclass(frozen=True)
class SecurityQueryParams:
    """Parameter bundle for security_query."""

    store: LocalLogStore | None = None
    root: str | Path | None = None
    attacker_ip: str | None = None
    victim_ip: str | None = None
    domain: str | None = None
    uri: str | None = None
    alert_type: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int | None = DEFAULT_QUERY_LIMIT
    max_limit: int | None = None


@dataclass(frozen=True)
class HuntIpParams:
    """Parameter bundle for hunt_ip."""

    role: str = "any"
    store: LocalLogStore | None = None
    root: str | Path | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int | None = DEFAULT_QUERY_LIMIT
    max_limit: int | None = None


def security_query(params: SecurityQueryParams) -> dict[str, Any]:
    """Execute a bounded security-event query and return the tool payload."""
    local_store = _store(params.store, params.root)
    result = execute_security_query(
        local_store,
        QueryCriteria(
            attacker_ip=params.attacker_ip,
            victim_ip=params.victim_ip,
            domain=params.domain,
            uri=params.uri,
            alert_type=params.alert_type,
            start_time=params.start_time,
            end_time=params.end_time,
            limit=params.limit,
        ),
        max_limit=params.max_limit,
    )
    return _tool_response(result)


def hunt_ip(
    ip: str,
    *,
    params: HuntIpParams | None = None,
    role: str = "any",
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
) -> dict[str, Any]:
    """Pivot around one IP as attacker, victim, or both."""
    hunt_params = params or HuntIpParams(
        role=role,
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
    )
    if hunt_params.role not in {"any", "attacker", "victim"}:
        raise ValueError("role must be one of: any, attacker, victim")
    if hunt_params.role in {"attacker", "victim"}:
        return _hunt_ip_single_role(ip, params=hunt_params)
    return _hunt_ip_any(ip, hunt_params)


def _hunt_ip_single_role(ip: str, *, params: HuntIpParams) -> dict[str, Any]:
    field = {"attacker": "attacker_ip", "victim": "victim_ip"}[params.role]
    return security_query(
        SecurityQueryParams(
            store=params.store,
            root=params.root,
            **{field: ip},
            start_time=params.start_time,
            end_time=params.end_time,
            limit=params.limit,
            max_limit=params.max_limit,
        )
    )


def _hunt_ip_any(ip: str, params: HuntIpParams) -> dict[str, Any]:
    local_store = _store(params.store, params.root)
    attacker = _hunt_role_query(local_store, params=params, attacker_ip=ip)
    victim = _hunt_role_query(local_store, params=params, victim_ip=ip)
    return _hunt_any_response(ip, attacker, victim)


def _hunt_role_query(
    local_store: LocalLogStore,
    *,
    params: HuntIpParams,
    attacker_ip: str | None = None,
    victim_ip: str | None = None,
) -> QueryResult:
    return execute_security_query(
        local_store,
        QueryCriteria(
            attacker_ip=attacker_ip,
            victim_ip=victim_ip,
            start_time=params.start_time,
            end_time=params.end_time,
            limit=params.limit,
        ),
        max_limit=params.max_limit,
    )


def _hunt_any_response(ip: str, attacker: QueryResult, victim: QueryResult) -> dict[str, Any]:
    return {
        "tool": "security_hunt_ip",
        "seed": {"ip": ip, "role": "any"},
        "queries": [_tool_response(attacker), _tool_response(victim)],
        "row_count": attacker.row_count + victim.row_count,
        "evidence_refs": [attacker.query_id, victim.query_id],
    }


def security_hunt_ip(
    ip: str,
    *,
    params: HuntIpParams | None = None,
    role: str = "any",
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the security_hunt_ip tool name."""
    return hunt_ip(
        ip,
        params=params,
        role=role,
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
    )


def security_hunt_domain(domain: str) -> dict[str, Any]:
    """Run a domain pivot through security_query."""
    return security_query(SecurityQueryParams(domain=domain))


def trace_case(
    case_id: str,
    *,
    params: TraceCaseParams | None = None,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> dict[str, Any]:
    """Trace related evidence by extracting query seeds from a saved case."""
    trace_params = params or trace_case_params(
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
        max_queries=max_queries,
    )
    local_store = _store(trace_params.store, trace_params.root)
    case = local_store.get_case(case_id)
    if case is None:
        raise KeyError(f"case not found: {case_id}")
    queries = trace_case_queries(local_store, _case_seeds(case), trace_params, _trace_field_query)
    return trace_case_response(case_id, queries, trace_params.max_queries)


def _trace_field_query(
    local_store: LocalLogStore,
    field: str,
    value: str,
    params: TraceCaseParams,
) -> dict[str, Any]:
    query_fields = {
        field: value,
        "start_time": params.start_time,
        "end_time": params.end_time,
        "limit": params.limit,
        "max_limit": params.max_limit,
    }
    return security_query(SecurityQueryParams(store=local_store, **query_fields))


def security_trace_case(
    case_id: str,
    *,
    params: TraceCaseParams | None = None,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> dict[str, Any]:
    """Compatibility wrapper for the security_trace_case tool name."""
    return trace_case(
        case_id,
        params=params,
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
        max_queries=max_queries,
    )


def _tool_response(result: QueryResult) -> dict[str, Any]:
    """Convert QueryResult to the compact agent tool response."""
    return {
        "tool": "security_query",
        "query_id": result.query_id,
        "parameters": result.parameters,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "evidence_path": result.evidence_path,
        "evidence_refs": [result.query_id],
        "summary": result.summary,
        "preview_rows": result.preview_rows,
    }


def _case_seeds(case: dict[str, Any]) -> dict[str, list[str]]:
    """Extract query seeds from top-level and nested case fields."""
    seeds: dict[str, list[str]] = {
        "attacker_ip": [],
        "victim_ip": [],
        "domain": [],
        "uri": [],
        "alert_type": [],
    }
    for container_name in ("entities", "attributes", "metadata"):
        container = case.get(container_name)
        _extend_container_seeds(seeds, container)
    for field in seeds:
        _extend_seed(seeds[field], case.get(field))
    return {field: values for field, values in seeds.items() if values}


def _extend_container_seeds(seeds: dict[str, list[str]], container: Any) -> None:
    if not isinstance(container, dict):
        return
    for field in seeds:
        _extend_seed(seeds[field], container.get(field))


def _extend_seed(target: list[str], value: Any) -> None:
    """Append a scalar or nested seed value to target in de-duplicated form."""
    if value in (None, ""):
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _extend_seed(target, item)
        return
    text = str(value)
    if text not in target:
        target.append(text)


def _store(store: LocalLogStore | None, root: str | Path | None) -> LocalLogStore:
    """Resolve a caller-provided store or create one from root."""
    if store is not None:
        return store
    return LocalLogStore(root)
