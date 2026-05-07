# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Parent-session health summaries for log-analysis dispatch."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ..agents.prompts import SecurityPromptConfig
from .budgets import DispatchBudget
from .queue import InvestigationQueue


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchHealthSummary 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchHealthSummary 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DispatchHealthSummary:
    case_backlog: dict[str, int] = field(default_factory=dict)
    agent_backlog: dict[str, Any] = field(default_factory=dict)
    prompt_switch: dict[str, Any] = field(default_factory=dict)
    dispatch_budget: dict[str, Any] = field(default_factory=dict)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchHealthInputs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchHealthInputs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DispatchHealthInputs:
    cases: list[Any] | None = None
    case_backlog: Mapping[str, int] | None = None
    queue: InvestigationQueue | None = None
    budget: DispatchBudget | Mapping[str, Any] | None = None
    prompt_config: SecurityPromptConfig | Mapping[str, Any] | None = None


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _get 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _case_backlog_from_cases 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 case backlog from cases 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _case_backlog_from_cases(cases: list[Any] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases or []:
        status = str(_get(case, "status", "unknown") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    if cases is not None:
        counts["total"] = len(cases)
    return counts


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 build_health_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build health summary 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_health_summary(
    *,
    params: DispatchHealthInputs | None = None,
    inputs: DispatchHealthInputs | None = None,
    cases: list[Any] | None = None,
    case_backlog: Mapping[str, int] | None = None,
    queue: InvestigationQueue | None = None,
    budget: DispatchBudget | Mapping[str, Any] | None = None,
    prompt_config: SecurityPromptConfig | Mapping[str, Any] | None = None,
) -> DispatchHealthSummary:
    health_inputs = params or inputs or DispatchHealthInputs(
        cases=cases,
        case_backlog=case_backlog,
        queue=queue,
        budget=budget,
        prompt_config=prompt_config,
    )
    budget = health_inputs.budget
    prompt_config = health_inputs.prompt_config
    dispatch_budget = budget if isinstance(budget, DispatchBudget) else DispatchBudget.from_mapping(budget)
    security_prompt = (
        prompt_config
        if isinstance(prompt_config, SecurityPromptConfig)
        else SecurityPromptConfig.from_mapping(prompt_config)
    )
    case_counts = (
        dict(health_inputs.case_backlog)
        if health_inputs.case_backlog is not None
        else _case_backlog_from_cases(health_inputs.cases)
    )
    agent_backlog = health_inputs.queue.agent_backlog() if health_inputs.queue is not None else {}
    return DispatchHealthSummary(
        case_backlog=case_counts,
        agent_backlog=agent_backlog,
        prompt_switch=security_prompt.to_dict(),
        dispatch_budget=dispatch_budget.to_dict(),
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 render_health_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 render health summary 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def render_health_summary(summary: DispatchHealthSummary | Mapping[str, Any]) -> str:
    payload = summary.to_dict() if isinstance(summary, DispatchHealthSummary) else dict(summary)
    case_backlog = payload.get("case_backlog", {})
    agent_backlog = payload.get("agent_backlog", {})
    prompt_switch = payload.get("prompt_switch", {})
    dispatch_budget = payload.get("dispatch_budget", {})
    return "\n".join(
        [
            "log_analysis_health:",
            f"- case_backlog: {case_backlog}",
            f"- agent_backlog: {agent_backlog}",
            (
                "- prompt_switch: "
                f"security_prompt_enabled={prompt_switch.get('security_prompt_enabled', False)} "
                f"security_prompt_mode={prompt_switch.get('security_prompt_mode', 'off')}"
            ),
            (
                "- dispatch_budget: "
                f"case_auto_dispatch_enabled={dispatch_budget.get('case_auto_dispatch_enabled', False)} "
                f"max_parallel_analyst_agents={dispatch_budget.get('max_parallel_analyst_agents', 0)} "
                f"analyst_agent_budget_per_hour={dispatch_budget.get('analyst_agent_budget_per_hour', 0)}"
            ),
        ]
    )
