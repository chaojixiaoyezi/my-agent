from __future__ import annotations

"""Safe tool-shaped helpers for local log-analysis hunting."""

from pathlib import Path
from typing import Any

from .storage import LocalLogStore, QueryCriteria, QueryResult, execute_security_query


def security_query(
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    attacker_ip: str | None = None,
    victim_ip: str | None = None,
    domain: str | None = None,
    uri: str | None = None,
    alert_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    local_store = _store(store, root)
    result = execute_security_query(
        local_store,
        QueryCriteria(
            attacker_ip=attacker_ip,
            victim_ip=victim_ip,
            domain=domain,
            uri=uri,
            alert_type=alert_type,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        ),
    )
    return _tool_response(result)


def hunt_ip(
    ip: str,
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    role: str = "any",
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if role not in {"any", "attacker", "victim"}:
        raise ValueError("role must be one of: any, attacker, victim")
    if role == "attacker":
        return security_query(
            store=store,
            root=root,
            attacker_ip=ip,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
    if role == "victim":
        return security_query(
            store=store,
            root=root,
            victim_ip=ip,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
    local_store = _store(store, root)
    attacker = execute_security_query(
        local_store,
        QueryCriteria(attacker_ip=ip, start_time=start_time, end_time=end_time, limit=limit),
    )
    victim = execute_security_query(
        local_store,
        QueryCriteria(victim_ip=ip, start_time=start_time, end_time=end_time, limit=limit),
    )
    return {
        "tool": "security_hunt_ip",
        "seed": {"ip": ip, "role": role},
        "queries": [_tool_response(attacker), _tool_response(victim)],
        "row_count": attacker.row_count + victim.row_count,
        "evidence_refs": [attacker.query_id, victim.query_id],
    }


def security_hunt_ip(ip: str, **kwargs: Any) -> dict[str, Any]:
    return hunt_ip(ip, **kwargs)


def security_hunt_domain(domain: str, **kwargs: Any) -> dict[str, Any]:
    return security_query(domain=domain, **kwargs)


def trace_case(
    case_id: str,
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    local_store = _store(store, root)
    case = local_store.get_case(case_id)
    if case is None:
        raise KeyError(f"case not found: {case_id}")
    seeds = _case_seeds(case)
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        for value in seeds.get(field, []):
            kwargs = {
                field: value,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
            }
            queries.append(security_query(store=local_store, **kwargs))
    return {
        "tool": "security_trace_case",
        "case_id": case_id,
        "query_count": len(queries),
        "row_count": sum(int(query["row_count"]) for query in queries),
        "queries": queries,
        "evidence_refs": [ref for query in queries for ref in query.get("evidence_refs", [])],
    }


def security_trace_case(case_id: str, **kwargs: Any) -> dict[str, Any]:
    return trace_case(case_id, **kwargs)


def _tool_response(result: QueryResult) -> dict[str, Any]:
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
    seeds: dict[str, list[str]] = {
        "attacker_ip": [],
        "victim_ip": [],
        "domain": [],
        "uri": [],
        "alert_type": [],
    }
    for container_name in ("entities", "attributes", "metadata"):
        container = case.get(container_name)
        if isinstance(container, dict):
            for field in seeds:
                _extend_seed(seeds[field], container.get(field))
    for field in seeds:
        _extend_seed(seeds[field], case.get(field))
    return {field: values for field, values in seeds.items() if values}


def _extend_seed(target: list[str], value: Any) -> None:
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
    if store is not None:
        return store
    return LocalLogStore(root)
