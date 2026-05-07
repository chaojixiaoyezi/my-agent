# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Security log query functions used by log-analysis tools.

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
    TraceCaseQueryRequest,
    TraceFieldQueryRequest,
    trace_case_params,
    trace_case_queries,
    trace_case_response,
)


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 SecurityQueryParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityQueryParams 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 HuntIpParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 HuntIpParams 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 security_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security query 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 hunt_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt ip 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _hunt_ip_single_role 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt ip single role 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _hunt_ip_any 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt ip any 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _hunt_ip_any(ip: str, params: HuntIpParams) -> dict[str, Any]:
    local_store = _store(params.store, params.root)
    attacker = _hunt_role_query(local_store, params=params, attacker_ip=ip)
    victim = _hunt_role_query(local_store, params=params, victim_ip=ip)
    return _hunt_any_response(ip, attacker, victim)


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _hunt_role_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt role query 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _hunt_any_response 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt any response 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _hunt_any_response(ip: str, attacker: QueryResult, victim: QueryResult) -> dict[str, Any]:
    return {
        "tool": "security_hunt_ip",
        "seed": {"ip": ip, "role": "any"},
        "queries": [_tool_response(attacker), _tool_response(victim)],
        "row_count": attacker.row_count + victim.row_count,
        "evidence_refs": [attacker.query_id, victim.query_id],
    }


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 security_hunt_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security hunt ip 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 security_hunt_domain 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security hunt domain 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def security_hunt_domain(domain: str) -> dict[str, Any]:
    """Run a domain pivot through security_query."""
    return security_query(SecurityQueryParams(domain=domain))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 trace_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace case 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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
    query_request = TraceCaseQueryRequest(local_store, _case_seeds(case), trace_params, _trace_field_query)
    queries = trace_case_queries(query_request)
    return trace_case_response(case_id, queries, trace_params.max_queries)


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _trace_field_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace field query 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _trace_field_query(request: TraceFieldQueryRequest) -> dict[str, Any]:
    params = request.params
    query_fields = {
        request.field: request.value,
        "start_time": params.start_time,
        "end_time": params.end_time,
        "limit": params.limit,
        "max_limit": params.max_limit,
    }
    return security_query(SecurityQueryParams(store=request.local_store, **query_fields))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 security_trace_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 security trace case 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _tool_response 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 tool response 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _case_seeds 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 case seeds 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _extend_container_seeds 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 extend container seeds 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _extend_container_seeds(seeds: dict[str, list[str]], container: Any) -> None:
    if not isinstance(container, dict):
        return
    for field in seeds:
        _extend_seed(seeds[field], container.get(field))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _extend_seed 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 extend seed 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _store 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 store 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _store(store: LocalLogStore | None, root: str | Path | None) -> LocalLogStore:
    """Resolve a caller-provided store or create one from root."""
    if store is not None:
        return store
    return LocalLogStore(root)
