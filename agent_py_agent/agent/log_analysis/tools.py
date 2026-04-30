from __future__ import annotations

"""Safe tool-shaped helpers for local log-analysis hunting."""

import json
from pathlib import Path
from typing import Any

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .storage import DEFAULT_QUERY_LIMIT, LocalLogStore, QueryCriteria, QueryResult, execute_security_query

MAX_TRACE_CASE_QUERIES = 20


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
    # TODO: Wire query_default_limit/query_max_limit from runtime config when that
    # configuration surface lands; storage.normalize_limit enforces MAX_QUERY_LIMIT.
    limit: int = DEFAULT_QUERY_LIMIT,
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
    limit: int = DEFAULT_QUERY_LIMIT,
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
    limit: int = DEFAULT_QUERY_LIMIT,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> dict[str, Any]:
    local_store = _store(store, root)
    case = local_store.get_case(case_id)
    if case is None:
        raise KeyError(f"case not found: {case_id}")
    seeds = _case_seeds(case)
    queries: list[dict[str, Any]] = []
    for field in ("attacker_ip", "victim_ip", "domain", "uri", "alert_type"):
        for value in seeds.get(field, []):
            if len(queries) >= max_queries:
                break
            kwargs = {
                field: value,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
            }
            queries.append(security_query(store=local_store, **kwargs))
        if len(queries) >= max_queries:
            break
    return {
        "tool": "security_trace_case",
        "case_id": case_id,
        "query_count": len(queries),
        "truncated": len(queries) >= max_queries,
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


class SecurityQueryTool(BaseTool):
    def __init__(self, store_root: Path):
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_query",
            category="log_analysis",
            description="Run a bounded local security-event query and return summary, preview rows, and evidence refs.",
            use_cases=[
                "Investigate logs with explicit time bounds and filters",
                "Create auditable evidence refs for analyst conclusions",
            ],
            avoid_when=[
                "Do not use for broad unbounded searches or to paste raw/full log rows into prompt"
            ],
            keywords=[
                "log",
                "logs",
                "security",
                "query",
                "evidence",
                "attacker_ip",
                "victim_ip",
                "domain",
                "alert_type",
            ],
            parameters={
                "start_time": "Required ISO timestamp lower bound.",
                "end_time": "Required ISO timestamp upper bound.",
                "attacker_ip": "Optional attacker/source IP filter.",
                "victim_ip": "Optional victim/destination IP filter.",
                "domain": "Optional domain/host filter.",
                "uri": "Optional URI/path/API filter.",
                "alert_type": "Optional alert type filter.",
                "limit": "Maximum evidence rows to persist; capped by storage.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_query","attacker_ip":"198.51.100.10","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":50}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        payload = security_query(root=params.get("root") or self.store_root, **_query_params(params))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


class SecurityHuntIpTool(BaseTool):
    def __init__(self, store_root: Path):
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_hunt_ip",
            category="log_analysis",
            description="Hunt a single IP as attacker, victim, or both with bounded security queries.",
            use_cases=[
                "Pivot from an IP in a case summary",
                "Collect evidence refs for attacker/victim activity in a time window",
            ],
            avoid_when=[
                "Do not use without a narrow time window or when a non-IP entity is the seed"
            ],
            keywords=["log", "logs", "security", "hunt", "ip", "attacker", "victim", "evidence"],
            parameters={
                "ip": "Required IP address seed.",
                "role": "Optional: any, attacker, or victim.",
                "start_time": "Required ISO timestamp lower bound.",
                "end_time": "Required ISO timestamp upper bound.",
                "limit": "Maximum evidence rows per query; capped by storage.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_hunt_ip","ip":"198.51.100.30","role":"any","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":25}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        ip = _required_text(params, "ip")
        payload = security_hunt_ip(ip, root=params.get("root") or self.store_root, **_hunt_params(params))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


class SecurityTraceCaseTool(BaseTool):
    def __init__(self, store_root: Path):
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_trace_case",
            category="log_analysis",
            description="Trace case entities with bounded queries and return evidence refs plus compact summaries.",
            use_cases=[
                "Expand a known case into auditable query refs",
                "Find related activity for case attacker/victim/domain/URI seeds",
            ],
            avoid_when=[
                "Do not use for full case dumps; read only summary and evidence refs in prompt"
            ],
            keywords=["log", "logs", "security", "case", "trace", "evidence", "incident"],
            parameters={
                "case_id": "Required case id from the local log-analysis store.",
                "start_time": "Required ISO timestamp lower bound.",
                "end_time": "Required ISO timestamp upper bound.",
                "limit": "Maximum evidence rows per query; capped by storage.",
                "max_queries": "Maximum related seed queries to run.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_trace_case","case_id":"case-1","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":25}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        case_id = _required_text(params, "case_id")
        payload = security_trace_case(
            case_id,
            root=params.get("root") or self.store_root,
            **_trace_params(params),
        )
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


def _query_params(params: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "attacker_ip",
        "victim_ip",
        "domain",
        "uri",
        "alert_type",
        "start_time",
        "end_time",
        "limit",
    )
    return {key: params[key] for key in keys if key in params}


def _hunt_params(params: dict[str, Any]) -> dict[str, Any]:
    payload = {key: params[key] for key in ("role", "start_time", "end_time", "limit") if key in params}
    return payload


def _trace_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: params[key] for key in ("start_time", "end_time", "limit", "max_queries") if key in params}


def _required_text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if value in (None, ""):
        raise ValueError(f"{key} is required")
    return str(value)


def _prompt_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
