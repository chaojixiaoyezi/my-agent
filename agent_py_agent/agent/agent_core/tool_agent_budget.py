# LLM: Per-agent tool budgets prevent repeat loops without throttling the whole task tree.
# 模块用途: 按 run_id 统计单个代理的滚动工具调用次数，超过预算时返回自检提示。

from __future__ import annotations

import time
from dataclasses import dataclass

from ..settings.runtime_guard_config import runtime_guard_int
from ..tools import ToolExecutionResult


# LLM: ToolAgentBudgetRequest bundles all data needed for rolling per-agent budget checks.
# 类用途: 保存一次工具预算检查的代理对象、run id、工具名和可测试时间戳。
@dataclass(frozen=True)
class ToolAgentBudgetRequest:
    agent: object
    run_id: str
    tool_name: str
    now: float | None = None


# LLM: check_tool_agent_budget is intentionally per-run and ignores main-agent calls without run_id.
# 函数用途: 检查单个代理是否超过滚动工具预算；未超过时登记本次调用，超过时返回自检提示。
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


# LLM: _events_by_run stores lightweight in-memory counters on the long-lived agent object.
# 函数用途: 获取或创建按 run_id 分组的工具调用时间戳，不写磁盘、不跨进程共享。
def _events_by_run(agent: object) -> dict[str, list[float]]:
    existing = getattr(agent, "_tool_agent_budget_events", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, list[float]] = {}
    agent._tool_agent_budget_events = created
    return created


# LLM: _budget_int reads explicit runtime config first, then the shared runtime guard file.
# 函数用途: 显式传入的配置优先；AgentConfig 默认不再写死预算数字，缺省时集中读取 runtime_guard_config.yaml。
def _budget_int(config: object, key: str, *, policy: object = None) -> int:
    value = getattr(config, key, None)
    if value is None:
        if hasattr(policy, "int_value"):
            return policy.int_value(key, 0)
        return runtime_guard_int(key, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: _budget_result tells the model to stop tool repetition and hand off if more tools are needed.
# 函数用途: 生成预算触发后的工具结果，让子代理用已有信息自检、汇报或请求父级接管。
def _budget_result(tool_name: str, run_id: str, max_calls: int, window_seconds: int) -> ToolExecutionResult:
    return ToolExecutionResult(
        str(tool_name or "unknown"),
        False,
        (
            f"单个代理工具预算已达到：run_id={run_id} 最近 {window_seconds} 秒最多 {max_calls} 次工具调用。"
            "请不要继续请求新工具；先自检是否在重复读取/写入/查询，基于已有工具结果总结当前进展、"
            "剩余缺口和下一步。如果确实还需要工具，请向父级上报需要继续调度、接管或提高预算的原因。"
        ),
    )
