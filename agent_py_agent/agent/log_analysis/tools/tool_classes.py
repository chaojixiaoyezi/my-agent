from __future__ import annotations

"""LLM: 本模块包含安全日志工具的 BaseTool 子类及其参数过滤辅助函数。

新手说明:
这里的类把普通 Python 函数包装成 agent 工具系统认识的 BaseTool。
spec 描述工具名字、用途、参数和例子；execute 负责真正调用函数并返回 ToolExecutionResult。
查询函数本身在 query_functions.py 里，本模块只负责"注册 + 调度"。
"""

import json
from pathlib import Path
from typing import Any

from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .query_functions import (
    SecurityQueryParams,
    security_hunt_ip,
    security_query,
    security_trace_case,
)


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
        payload = security_query(SecurityQueryParams(
            root=params.get("root") or self.store_root,
            **_query_params(params),
        ))
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
