# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""本模块包含安全日志工具的 BaseTool 子类及其参数过滤辅助函数。

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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 SecurityQueryTool 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 SecurityQueryTool 的状态和协作方法，作为当前模块对外复用的领域对象。
class SecurityQueryTool(BaseTool):
    """Tool wrapper for bounded log-analysis queries."""

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, store_root: Path):
        """Initialize the tool spec and default store root."""
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_query",
            category="log_analysis",
            # LLM: LOG query tools are read-only; registry Tool Manifest Gate depends on this machine field.
            effect="read_only",
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

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 execute 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 execute 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """Execute the tool and return prompt-safe JSON."""
        payload = security_query(SecurityQueryParams(
            root=params.get("root") or self.store_root,
            **_query_params(params),
        ))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 SecurityHuntIpTool 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 SecurityHuntIpTool 的状态和协作方法，作为当前模块对外复用的领域对象。
class SecurityHuntIpTool(BaseTool):
    """Tool wrapper for IP pivot hunting."""

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, store_root: Path):
        """Initialize the tool spec and default store root."""
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_hunt_ip",
            category="log_analysis",
            # LLM: LOG hunt tools read local evidence only; they must not be treated as mutating actions.
            effect="read_only",
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

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 execute 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 execute 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """Execute the tool and return prompt-safe JSON."""
        ip = _required_text(params, "ip")
        payload = security_hunt_ip(ip, root=params.get("root") or self.store_root, **_hunt_params(params))
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 SecurityTraceCaseTool 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 SecurityTraceCaseTool 的状态和协作方法，作为当前模块对外复用的领域对象。
class SecurityTraceCaseTool(BaseTool):
    """Tool wrapper for case trace expansion."""

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, store_root: Path):
        """Initialize the tool spec and default store root."""
        self.store_root = store_root
        self.spec = ToolSpec(
            name="security_trace_case",
            category="log_analysis",
            # LLM: Trace-case expansion is a read-only query wrapper; side-effect gates consume this flag.
            effect="read_only",
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

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 execute 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 execute 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        """Execute the tool and return prompt-safe JSON."""
        case_id = _required_text(params, "case_id")
        payload = security_trace_case(
            case_id,
            root=params.get("root") or self.store_root,
            **_trace_params(params),
        )
        return ToolExecutionResult(self.spec.name, True, _prompt_json(payload))


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _query_params 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query params 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_params(params: dict[str, Any]) -> dict[str, Any]:
    """Tool wrapper for bounded log-analysis queries."""
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


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _hunt_params 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 hunt params 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _hunt_params(params: dict[str, Any]) -> dict[str, Any]:
    """Tool wrapper for bounded log-analysis queries."""
    payload = {key: params[key] for key in ("role", "start_time", "end_time", "limit", "max_limit") if key in params}
    return payload


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _trace_params 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 trace params 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _trace_params(params: dict[str, Any]) -> dict[str, Any]:
    """Tool wrapper for bounded log-analysis queries."""
    return {key: params[key] for key in ("start_time", "end_time", "limit", "max_limit", "max_queries") if key in params}


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _required_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 required text 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _required_text(params: dict[str, Any], key: str) -> str:
    """Tool wrapper for bounded log-analysis queries."""
    value = params.get(key)
    if value in (None, ""):
        raise ValueError(f"{key} is required")
    return str(value)


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 _prompt_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 prompt json 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def _prompt_json(payload: dict[str, Any]) -> str:
    """Tool wrapper for bounded log-analysis queries."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
