
from __future__ import annotations

import time
from dataclasses import dataclass

from ...settings.runtime_guard_config import runtime_guard_int
from ...tooling.models import ToolExecutionResult


@dataclass(frozen=True)
class ToolAgentBudgetRequest:
    agent: object
    run_id: str
    tool_name: str
    now: float | None = None


def check_tool_agent_budget(request: ToolAgentBudgetRequest) -> ToolExecutionResult | None:
    config = getattr(request.agent, "config", None)
    policy = getattr(request.agent, "runtime_guard_policy", None)
    run_id = str(request.run_id or "").strip()
    max_calls = _budget_int(config, "tool_agent_budget_max_calls", policy=policy)
    window_seconds = _budget_int(config, "tool_agent_budget_window_seconds", policy=policy)
    if not run_id or max_calls <= 0 or window_seconds <= 0:
        return None

    now = float(time.monotonic() if request.now is None else request.now)
    events_by_run = _events_by_run(request.agent)
    events = [item for item in events_by_run.get(run_id, []) if item > now - window_seconds]
    if len(events) >= max_calls:
        events_by_run[run_id] = events
        return _budget_result(request.tool_name, run_id, max_calls, window_seconds)
    events.append(now)
    events_by_run[run_id] = events
    return None


def _events_by_run(agent: object) -> dict[str, list[float]]:
    existing = getattr(agent, "_tool_agent_budget_events", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, list[float]] = {}
    agent._tool_agent_budget_events = created
    return created


def _budget_int(config: object, key: str, *, policy: object = None) -> int:
    if hasattr(config, key):
        value = getattr(config, key, None)
        if value is None:
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
    value = getattr(config, key, None)
    if value is None:
        if hasattr(policy, "int_value"):
            return policy.int_value(key, 0)
        return runtime_guard_int(key, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _budget_result(tool_name: str, run_id: str, max_calls: int, window_seconds: int) -> ToolExecutionResult:
    return ToolExecutionResult(
        str(tool_name or "unknown"),
        False,
        (
            f"单个代理工具预算已达到：run_id={run_id} 最近 {window_seconds} 秒最多 {max_calls} 次工具调用。"
            "请不要继续重复请求新工具；先自检是否已经掌握足够事实。"
            "如果足够完成当前交付，请立刻用已有工具结果写入交付物并给出最终回复；"
            "只有确实缺少关键事实时，才简短说明具体缺口和需要继续调度、接管或提高预算的原因。"
        ),
    )
