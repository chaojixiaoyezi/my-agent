from __future__ import annotations

"""LLM: 本模块把本地 LOG 查询能力包装成安全工具，返回摘要、预览行和 evidence refs，而不是整包原始日志。

新手说明:
这里的函数和类是 agent 能调用的“安全日志工具”。它们不会把所有日志直接塞进 prompt，
而是先按 IP、域名、时间窗口等条件查询本地 store，再把结果写成可追溯证据引用。
这样 analyst 可以引用 evidence refs 做判断，父会话也能回头检查证据。
"""

import json
from pathlib import Path
from typing import Any

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .storage import (
    DEFAULT_QUERY_LIMIT,
    LocalLogStore,
    QueryCriteria,
    QueryResult,
    execute_security_query,
)

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
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
) -> dict[str, Any]:
    """LLM: 执行一次有边界的安全事件查询，并把 QueryResult 转成工具返回格式。

    新手说明:
    这个函数像“查日志”的总入口。你可以按 attacker_ip、victim_ip、domain、uri、
    alert_type 和时间范围过滤事件。它返回的是摘要、预览行和 evidence ref，不是全量日志。

    参数说明:
    store: 已经创建好的 LocalLogStore；传它时函数直接用这个 store。
    root: 本地日志分析数据目录；store 没传时，用 root 创建 LocalLogStore。
    attacker_ip: 攻击者或源 IP 过滤条件。
    victim_ip: 受害者或目标 IP 过滤条件。
    domain: 域名或主机名过滤条件。
    uri: URI、路径或 API 过滤条件。
    alert_type: 告警类型过滤条件，例如 waf_block 或 suspicious_login。
    start_time: ISO 时间字符串，查询下界；安全分析里通常应该传。
    end_time: ISO 时间字符串，查询上界；和 start_time 一起限制窗口。
    limit: 本次最多持久化多少条证据行；None 时由下层默认处理。
    max_limit: 配置层允许的最大上限，用来防止调用方把 limit 放得过大。

    返回说明:
    返回 dict，包含 tool、query_id、parameters、row_count、truncated、evidence_path、
    evidence_refs、summary 和 preview_rows。后续报告应引用 evidence_refs，而不是凭空下结论。
    """
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
        max_limit=max_limit,
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
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
) -> dict[str, Any]:
    """LLM: 围绕单个 IP 做 attacker/victim/both 方向的受控 pivot 查询。

    新手说明:
    这个函数用于“我看到一个可疑 IP，想看看它在日志里还做了什么”。role 控制这个 IP
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
            max_limit=max_limit,
        )
    if role == "victim":
        return security_query(
            store=store,
            root=root,
            victim_ip=ip,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            max_limit=max_limit,
        )
    local_store = _store(store, root)
    attacker = execute_security_query(
        local_store,
        QueryCriteria(attacker_ip=ip, start_time=start_time, end_time=end_time, limit=limit),
        max_limit=max_limit,
    )
    victim = execute_security_query(
        local_store,
        QueryCriteria(victim_ip=ip, start_time=start_time, end_time=end_time, limit=limit),
        max_limit=max_limit,
    )
    return {
        "tool": "security_hunt_ip",
        "seed": {"ip": ip, "role": role},
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


def security_hunt_domain(domain: str, **kwargs: Any) -> dict[str, Any]:
    """LLM: 以 domain 为种子复用 security_query。

    新手说明:
    这是域名 pivot 的小包装。它不单独实现查询逻辑，只是把 domain 参数放进 security_query。

    参数说明:
    domain: 必填，域名或主机名。
    **kwargs: 透传给 security_query 的 store、root、时间窗口、limit、max_limit 等。

    返回说明:
    返回 security_query 的结果。
    """
    return security_query(domain=domain, **kwargs)


def trace_case(
    case_id: str,
    *,
    store: LocalLogStore | None = None,
    root: str | Path | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int | None = DEFAULT_QUERY_LIMIT,
    max_limit: int | None = None,
    max_queries: int = MAX_TRACE_CASE_QUERIES,
) -> dict[str, Any]:
    """LLM: 从已保存 case 中提取实体种子，按多个字段追踪相关日志证据。

    新手说明:
    case 里通常有 attacker_ip、victim_ip、domain、uri、alert_type 等线索。这个函数把这些线索
    拿出来逐个查询，形成一组 evidence refs，帮助 analyst 从“一个 case”扩展到“相关活动”。

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


class SecurityQueryTool(BaseTool):
    """LLM: Tool registry wrapper for bounded security_query execution.

    新手说明:
    这个类把普通 Python 函数 security_query 包装成 agent 工具系统认识的 BaseTool。
    spec 描述工具名字、用途、参数和例子；execute 负责真正调用函数并返回 ToolExecutionResult。

    字段说明:
    store_root: 默认日志分析数据目录，execute 没有传 root 时使用。
    spec: ToolSpec，告诉 agent 这个工具什么时候该用、参数是什么、什么时候不该用。
    """

    def __init__(self, store_root: Path):
        """LLM: 初始化 security_query 工具规格和默认 store root。

        新手说明:
        创建工具对象时先准备好说明书 spec，后面工具目录才能展示它。

        参数说明:
        store_root: 本地日志分析数据目录，作为 execute 的默认 root。
        """
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
                "limit": "Maximum evidence rows to persist.",
                "max_limit": "Optional configured upper bound for evidence rows.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_query","attacker_ip":"198.51.100.10","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":50}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """LLM: 从工具参数中抽取查询条件，调用 security_query，并把结果序列化给 prompt。

        新手说明:
        工具系统传进来的是 params 字典。这个方法负责取出允许的参数，忽略其它杂项，
        然后把查询结果变成 JSON 字符串。

        参数说明:
        params: 工具调用参数，可能包含 root、attacker_ip、victim_ip、domain、uri、alert_type、时间窗口和 limit。

        返回说明:
        返回 ToolExecutionResult，success=True 时 content 是格式化 JSON。
        """
        payload = security_query(root=params.get("root") or self.store_root, **_query_params(params))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


class SecurityHuntIpTool(BaseTool):
    """LLM: Tool registry wrapper for IP pivot hunting.

    新手说明:
    这个类把 security_hunt_ip 暴露给 agent。它要求必须传 ip，并允许 role 控制查询方向。

    字段说明:
    store_root: 默认日志分析数据目录。
    spec: ToolSpec，描述 IP hunting 的参数、例子和禁用场景。
    """

    def __init__(self, store_root: Path):
        """LLM: 初始化 security_hunt_ip 工具规格和默认 store root。

        新手说明:
        spec 里的参数说明会展示给 agent，帮助它知道 ip、role、时间窗口怎么传。

        参数说明:
        store_root: 本地日志分析数据目录，作为 execute 的默认 root。
        """
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
                "limit": "Maximum evidence rows per query.",
                "max_limit": "Optional configured upper bound for evidence rows.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_hunt_ip","ip":"198.51.100.30","role":"any","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":25}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """LLM: 校验必填 ip，调用 security_hunt_ip，并返回 JSON 工具结果。

        新手说明:
        如果没有 ip，这个方法会抛 ValueError。这样错误会尽早暴露，而不是跑一个空查询。

        参数说明:
        params: 工具调用参数，必须包含 ip，可选 role、root、start_time、end_time、limit、max_limit。

        返回说明:
        返回 ToolExecutionResult，content 是 security_hunt_ip 的 JSON 结果。
        """
        ip = _required_text(params, "ip")
        payload = security_hunt_ip(ip, root=params.get("root") or self.store_root, **_hunt_params(params))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


class SecurityTraceCaseTool(BaseTool):
    """LLM: Tool registry wrapper for case-based trace expansion.

    新手说明:
    这个类把本地 case 追踪能力暴露给 agent。它不是读取完整 case 给模型，
    而是根据 case 里的种子跑受控查询，返回 evidence refs。

    字段说明:
    store_root: 默认日志分析数据目录。
    spec: ToolSpec，描述 case trace 的参数、例子和禁用场景。
    """

    def __init__(self, store_root: Path):
        """LLM: 初始化 security_trace_case 工具规格和默认 store root。

        新手说明:
        spec 告诉 agent 需要 case_id，并提醒不要把它当作 full case dump 工具。

        参数说明:
        store_root: 本地日志分析数据目录，作为 execute 的默认 root。
        """
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
                "limit": "Maximum evidence rows per query.",
                "max_limit": "Optional configured upper bound for evidence rows.",
                "max_queries": "Maximum related seed queries to run.",
                "root": "Optional log-analysis store root.",
            },
            examples=[
                '{"tool":"security_trace_case","case_id":"case-1","start_time":"2026-04-30T09:00:00Z","end_time":"2026-04-30T11:00:00Z","limit":25}'
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """LLM: 校验必填 case_id，调用 security_trace_case，并返回 JSON 工具结果。

        新手说明:
        这个方法把工具调用参数翻译给 trace_case。case_id 缺失会立刻报错。

        参数说明:
        params: 工具调用参数，必须包含 case_id，可选 root、start_time、end_time、limit、max_limit、max_queries。

        返回说明:
        返回 ToolExecutionResult，content 是 security_trace_case 的 JSON 结果。
        """
        case_id = _required_text(params, "case_id")
        payload = security_trace_case(
            case_id,
            root=params.get("root") or self.store_root,
            **_trace_params(params),
        )
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


def _query_params(params: dict[str, Any]) -> dict[str, Any]:
    """LLM: 从工具 params 中挑出 security_query 允许的参数。

    新手说明:
    工具调用字典里可能混入其它字段。这个函数只保留查询函数认识的键，减少误传。

    参数说明:
    params: 原始工具参数字典。

    返回说明:
    返回过滤后的 dict，可安全传给 security_query。
    """
    keys = (
        "attacker_ip",
        "victim_ip",
        "domain",
        "uri",
        "alert_type",
        "start_time",
        "end_time",
        "limit",
        "max_limit",
    )
    return {key: params[key] for key in keys if key in params}


def _hunt_params(params: dict[str, Any]) -> dict[str, Any]:
    """LLM: 从工具 params 中挑出 hunt_ip 允许的参数。

    新手说明:
    IP hunting 只需要 role、时间窗口和 limit 这些参数，其它字段不该传进去。

    参数说明:
    params: 原始工具参数字典。

    返回说明:
    返回过滤后的 dict，可安全传给 security_hunt_ip。
    """
    payload = {key: params[key] for key in ("role", "start_time", "end_time", "limit", "max_limit") if key in params}
    return payload


def _trace_params(params: dict[str, Any]) -> dict[str, Any]:
    """LLM: 从工具 params 中挑出 trace_case 允许的参数。

    新手说明:
    case trace 除了时间和 limit，还允许 max_queries 控制最多扩展多少个 seed。

    参数说明:
    params: 原始工具参数字典。

    返回说明:
    返回过滤后的 dict，可安全传给 security_trace_case。
    """
    return {key: params[key] for key in ("start_time", "end_time", "limit", "max_limit", "max_queries") if key in params}


def _required_text(params: dict[str, Any], key: str) -> str:
    """LLM: 校验工具参数中的必填文本字段。

    新手说明:
    有些参数比如 ip、case_id 不能缺。这个函数负责统一报错。

    参数说明:
    params: 原始工具参数字典。
    key: 必填字段名。

    返回说明:
    返回字符串形式的参数值。

    异常说明:
    字段不存在或是空字符串时抛 ValueError。
    """
    value = params.get(key)
    if value in (None, ""):
        raise ValueError(f"{key} is required")
    return str(value)


def _prompt_json(payload: dict[str, Any]) -> str:
    """LLM: 把工具 payload 转成稳定、可读、保留中文的 JSON 字符串。

    新手说明:
    agent prompt 里最好看到格式化 JSON，便于模型引用字段，也方便人类调试。

    参数说明:
    payload: 要输出给工具结果的字典。

    返回说明:
    返回带缩进、key 排序、中文不转义的 JSON 字符串。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
