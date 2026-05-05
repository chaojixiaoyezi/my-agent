from __future__ import annotations

"""LLM: 本模块包含所有日志安全查询函数及其内部辅助工具，负责有边界的查询执行、IP/域名追踪和 case 扩展。

新手说明:
这里的函数是 agent 能调用的"安全日志查询"核心逻辑。它们不会把所有日志直接塞进 prompt，
而是先按 IP、域名、时间窗口等条件查询本地 store，再把结果写成可追溯证据引用。
这样 analyst 可以引用 evidence refs 做判断，父会话也能回头检查证据。
本模块只包含函数，工具类（BaseTool 子类）在 tool_classes.py 里。
"""

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

MAX_TRACE_CASE_QUERIES = 20


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


def security_query(
    params: SecurityQueryParams,
) -> dict[str, Any]:
    """LLM: 执行一次有边界的安全事件查询，并把 QueryResult 转成工具返回格式。

    新手说明:
    这个函数像"查日志"的总入口。你可以按 attacker_ip、victim_ip、domain、uri、
    alert_type 和时间范围过滤事件。它返回的是摘要、预览行和 evidence ref，不是全量日志。

    参数说明:
    params: SecurityQueryParams dataclass，包含 store、root、各过滤条件、时间窗口、limit。

    返回说明:
    返回 dict，包含 tool、query_id、parameters、row_count、truncated、evidence_path、
    evidence_refs、summary 和 preview_rows。后续报告应引用 evidence_refs，而不是凭空下结论。
    """
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


def hunt_ip(ip: str, **kwargs: Any) -> dict[str, Any]:
    """LLM: 围绕单个 IP 做 attacker/victim/both 方向的受控 pivot 查询。

    新手说明:
    这个函数用于"我看到一个可疑 IP，想看看它在日志里还做了什么"。role 控制这个 IP
    被当作攻击者、受害者，还是两边都查。role=any 会跑两次查询并汇总证据。

    参数说明:
    ip: 必填，作为查询种子的 IP 地址。
    store: 已创建的 LocalLogStore；有它就不再用 root 创建。
    root: 本地日志分析数据目录；store 为空时使用。
    role: "attacker" 只查 attacker_ip，"victim" 只查 victim_ip，"any" 两边都查。
    start_time: ISO 时间字符串，查询下界。
    end_time: ISO 时间字符串，查询上界。
    limit: 每次查询最多持久化多少条证据行。
    max_limit: 配置层允许的最大行数上限。

    返回说明:
    role 是 attacker/victim 时返回 security_query 的结果；role=any 时返回两次查询的组合，
    包含 queries、row_count 和 evidence_refs。

    异常说明:
    role 不属于 any/attacker/victim 时抛 ValueError，避免悄悄按错误方向查询。
    """
    store = kwargs.get("store")
    root = kwargs.get("root")
    role = kwargs.get("role", "any")
    start_time = kwargs.get("start_time")
    end_time = kwargs.get("end_time")
    limit = kwargs.get("limit", DEFAULT_QUERY_LIMIT)
    max_limit = kwargs.get("max_limit")
    if role not in {"any", "attacker", "victim"}:
        raise ValueError("role must be one of: any, attacker, victim")
    if role in {"attacker", "victim"}:
        return _hunt_ip_single_role(ip, role=role, store=store, root=root, start_time=start_time, end_time=end_time, limit=limit, max_limit=max_limit)
    return _hunt_ip_any(
        ip,
        store=store,
        root=root,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        max_limit=max_limit,
    )


def _hunt_ip_single_role(ip: str, **kwargs: Any) -> dict[str, Any]:
    field = {"attacker": "attacker_ip", "victim": "victim_ip"}[str(kwargs["role"])]
    return security_query(
        SecurityQueryParams(
            store=kwargs.get("store"),
            root=kwargs.get("root"),
            **{field: ip},
            start_time=kwargs.get("start_time"),
            end_time=kwargs.get("end_time"),
            limit=kwargs.get("limit"),
            max_limit=kwargs.get("max_limit"),
        )
    )


def _hunt_ip_any(ip: str, **kwargs: Any) -> dict[str, Any]:
    local_store = _store(kwargs.get("store"), kwargs.get("root"))
    attacker = _hunt_role_query(local_store, attacker_ip=ip, **kwargs)
    victim = _hunt_role_query(local_store, victim_ip=ip, **kwargs)
    return _hunt_any_response(ip, attacker, victim)


def _hunt_role_query(local_store: LocalLogStore, **kwargs: Any) -> QueryResult:
    return execute_security_query(
        local_store,
        QueryCriteria(
            attacker_ip=kwargs.get("attacker_ip"),
            victim_ip=kwargs.get("victim_ip"),
            start_time=kwargs.get("start_time"),
            end_time=kwargs.get("end_time"),
            limit=kwargs.get("limit"),
        ),
        max_limit=kwargs.get("max_limit"),
    )


def _hunt_any_response(ip: str, attacker: QueryResult, victim: QueryResult) -> dict[str, Any]:
    return {
        "tool": "security_hunt_ip",
        "seed": {"ip": ip, "role": "any"},
        "queries": [_tool_response(attacker), _tool_response(victim)],
        "row_count": attacker.row_count + victim.row_count,
        "evidence_refs": [attacker.query_id, victim.query_id],
    }


def security_hunt_ip(ip: str, **kwargs: Any) -> dict[str, Any]:
    """LLM: 工具名兼容包装，把 security_hunt_ip 调用转给 hunt_ip。

    新手说明:
    agent 工具目录里叫 security_hunt_ip，但内部实现函数叫 hunt_ip。

    参数说明:
    ip: 必填 IP 种子。
    **kwargs: 透传给 hunt_ip 的 role、store、root、start_time、end_time、limit、max_limit。

    返回说明:
    返回 hunt_ip 的结果。
    """
    return hunt_ip(ip, **kwargs)


def security_hunt_domain(domain: str) -> dict[str, Any]:
    """LLM: 以 domain 为种子复用 security_query。

    新手说明:
    这是域名 pivot 的小包装。它不单独实现查询逻辑，只是把 domain 参数放进 security_query。

    参数说明:
    domain: 必填，域名或主机名。

    返回说明:
    返回 security_query 的结果。
    """
    return security_query(SecurityQueryParams(domain=domain))


def trace_case(case_id: str, **kwargs: Any) -> dict[str, Any]:
    """LLM: 从已保存 case 中提取实体种子，按多个字段追踪相关日志证据。

    新手说明:
    case 里通常有 attacker_ip、victim_ip、domain、uri、alert_type 等线索。这个函数把这些线索
    拿出来逐个查询，形成一组 evidence refs，帮助 analyst 从"一个 case"扩展到"相关活动"。

    参数说明:
    case_id: 必填，本地 store 里的 case id。
    store: 已创建的 LocalLogStore；有它就直接使用。
    root: 本地日志分析数据目录；store 为空时使用。
    start_time: ISO 时间字符串，追踪查询下界。
    end_time: ISO 时间字符串，追踪查询上界。
    limit: 每个 seed 查询最多持久化多少条证据行。
    max_limit: 配置层允许的最大行数上限。
    max_queries: 最多跑多少个 seed 查询，默认 MAX_TRACE_CASE_QUERIES，防止 case 太大时无限扩展。

    返回说明:
    返回 dict，包含 case_id、query_count、truncated、row_count、queries 和 evidence_refs。
    truncated=True 表示因为 max_queries 达到上限，后续 seed 没继续查询。

    异常说明:
    找不到 case_id 时抛 KeyError，调用方应先确认 case 已落盘。
    """
    store = kwargs.get("store")
    root = kwargs.get("root")
    start_time = kwargs.get("start_time")
    end_time = kwargs.get("end_time")
    limit = kwargs.get("limit", DEFAULT_QUERY_LIMIT)
    max_limit = kwargs.get("max_limit")
    max_queries = int(kwargs.get("max_queries", MAX_TRACE_CASE_QUERIES))
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
                "max_limit": max_limit,
            }
            queries.append(security_query(SecurityQueryParams(store=local_store, **kwargs)))
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
    """LLM: 工具名兼容包装，把 security_trace_case 调用转给 trace_case。

    新手说明:
    agent 工具目录里叫 security_trace_case，内部复用 trace_case 的实现。

    参数说明:
    case_id: 必填，本地 case id。
    **kwargs: 透传给 trace_case 的 store、root、时间窗口、limit、max_limit、max_queries。

    返回说明:
    返回 trace_case 的结果。
    """
    return trace_case(case_id, **kwargs)


def _tool_response(result: QueryResult) -> dict[str, Any]:
    """LLM: 把 QueryResult 转成 agent 工具协议需要的紧凑 JSON dict。

    新手说明:
    查询层返回的是 Python 对象，工具层需要能 JSON 序列化的字典。
    这里统一字段名，确保每个查询都带 query_id 和 evidence_refs。

    参数说明:
    result: execute_security_query 返回的 QueryResult。

    返回说明:
    返回工具响应 dict。evidence_refs 目前包含 query_id，供报告和 reviewer 回查证据。
    """
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
    """LLM: 从 case 的顶层字段和 entities/attributes/metadata 中提取可查询种子。

    新手说明:
    不同 case 可能把 IP、域名等线索放在不同位置。这个函数把这些可能的位置扫一遍，
    提取出 trace_case 可以使用的 attacker_ip、victim_ip、domain、uri、alert_type 列表。

    参数说明:
    case: 本地 store 读出的 case 字典。

    返回说明:
    返回字段到 seed 列表的 dict；没有 seed 的字段会被删掉。
    """
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
    """LLM: 把单个值或列表值归一成去重字符串 seed，追加到 target。

    新手说明:
    case 里的线索可能是一个字符串，也可能是列表、tuple、set，甚至是空值。
    这个函数负责把它们整理成字符串列表，并避免重复。

    参数说明:
    target: 要追加 seed 的列表，会被原地修改。
    value: 可能的 seed 值；None、空字符串会被忽略，集合/列表会递归展开。

    返回说明:
    没有返回值；结果写进 target。
    """
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
    """LLM: 统一解析 LocalLogStore，优先使用调用方传入的 store，否则按 root 创建。

    新手说明:
    测试里常常会传一个临时 store，真实 CLI 可能只传 root 路径。这个函数让其它工具不用重复写判断。

    参数说明:
    store: 已经创建好的 LocalLogStore。
    root: 本地日志分析数据目录；store 为空时使用。

    返回说明:
    返回 LocalLogStore 实例。
    """
    if store is not None:
        return store
    return LocalLogStore(root)
