from __future__ import annotations

"""Parent-session health summaries for log-analysis dispatch."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ..agents.prompts import SecurityPromptConfig
from .budgets import DispatchBudget
from .queue import InvestigationQueue


@dataclass
class DispatchHealthSummary:
    case_backlog: dict[str, int] = field(default_factory=dict)
    agent_backlog: dict[str, Any] = field(default_factory=dict)
    prompt_switch: dict[str, Any] = field(default_factory=dict)
    dispatch_budget: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DispatchHealthInputs:
    cases: list[Any] | None = None
    case_backlog: Mapping[str, int] | None = None
    queue: InvestigationQueue | None = None
    budget: DispatchBudget | Mapping[str, Any] | None = None
    prompt_config: SecurityPromptConfig | Mapping[str, Any] | None = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> DispatchHealthInputs:
        return cls(
            cases=kwargs.get("cases"),
            case_backlog=kwargs.get("case_backlog"),
            queue=kwargs.get("queue"),
            budget=kwargs.get("budget"),
            prompt_config=kwargs.get("prompt_config"),
        )


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_backlog_from_cases(cases: list[Any] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases or []:
        status = str(_get(case, "status", "unknown") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    if cases is not None:
        counts["total"] = len(cases)
    return counts


def build_health_summary(
    *,
    inputs: DispatchHealthInputs | None = None,
    **kwargs: Any,
) -> DispatchHealthSummary:
    health_inputs = inputs or DispatchHealthInputs.from_kwargs(**kwargs)
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
