
from __future__ import annotations

import time
from dataclasses import dataclass

from ...contracts.gates.command_policy import analyze_command
from ...settings.runtime_guard_config import runtime_guard_int
from ...tooling.models import ToolHandlerOutcome


@dataclass(frozen=True)
class ToolAgentBudgetRequest:
    agent: object
    run_id: str
    tool_name: str
    now: float | None = None


def check_tool_agent_budget(request: ToolAgentBudgetRequest) -> ToolHandlerOutcome | None:
    """Rolling-window tool budget scoped per agent instance (not per run_id).

    用户规格（2026-08-07 真机测试）：额度按每个代理实例独立计数——用户A的
    1 个主代理与 4 个子代理各拥有自己的窗口额度，互不挤占；用户间也不共用。
    agent 实例本身即桶：主代理所有 run 共享该主代理的额度，每个子代理同理。
    """
    config = getattr(request.agent, "config", None)
    policy = getattr(request.agent, "runtime_guard_policy", None)
    max_calls = _budget_int(config, "tool_agent_budget_max_calls", policy=policy)
    window_seconds = _budget_int(config, "tool_agent_budget_window_seconds", policy=policy)
    if max_calls <= 0 or window_seconds <= 0:
        return None

    now = float(time.monotonic() if request.now is None else request.now)
    events = _agent_events(request.agent)
    events[:] = [item for item in events if item > now - window_seconds]
    if len(events) >= max_calls:
        return _budget_result(request.tool_name, max_calls, window_seconds)
    events.append(now)
    return None


def check_unknown_command_budget(
    agent: object,
    tool_name: str,
    command_text: object,
    now: float | None = None,
) -> ToolHandlerOutcome | None:
    """Per-agent rolling budget for unknown (non-allowlisted) commands.

    unknown 命令不做人工审批（approval_request 无消费端，ask 只会卡死任务）。
    部署者可配置 unknown_command_allowlist（白名单内免额度），其余 unknown 命令
    走 agent 级滚动窗口额度（unknown_command_max_calls 默认 200 /
    unknown_command_window_seconds 默认 600，0=关闭）；超限暂时拒绝，
    窗口滚动过自动恢复。普通用户零负担：不配任何东西也自动生效。
    """
    if str(tool_name or "") not in _COMMAND_POLICY_TOOL_NAMES:
        return None
    analysis = analyze_command(command_text)
    if analysis.classification != "unknown":
        return None
    allowlist = _unknown_command_allowlist(agent)
    executables = tuple(segment.executable for segment in analysis.segments)
    if executables and allowlist and all(item in allowlist for item in executables):
        return None
    policy = getattr(agent, "runtime_guard_policy", None)
    max_calls = _budget_int(None, "unknown_command_max_calls", policy=policy, default=200)
    window_seconds = _budget_int(
        None, "unknown_command_window_seconds", policy=policy, default=600
    )
    if max_calls <= 0 or window_seconds <= 0:
        return None
    stamp = float(time.monotonic() if now is None else now)
    events = _unknown_command_events(agent)
    events[:] = [item for item in events if item > stamp - window_seconds]
    if len(events) >= max_calls:
        return ToolHandlerOutcome(
            str(tool_name or "unknown"),
            False,
            (
                f"unknown 命令窗口额度已用完：本代理最近 {window_seconds} 秒最多 {max_calls} "
                "次未声明命令。等窗口滚动过去再重试；如果这是常用的可信命令，"
                "部署者可以把它加进 unknown_command_allowlist 白名单。"
            ),
            error_code="TOOL_RATE_LIMIT_EXCEEDED",
        )
    events.append(stamp)
    return None


_COMMAND_POLICY_TOOL_NAMES = frozenset({"run_command"})


def _unknown_command_allowlist(agent: object) -> frozenset[str]:
    policy = getattr(agent, "runtime_guard_policy", None)
    values = getattr(policy, "values", None)
    if not isinstance(values, dict):
        return frozenset()
    raw = values.get("unknown_command_allowlist")
    items = raw if isinstance(raw, (list, tuple)) else ()
    return frozenset(str(item).strip() for item in items if str(item).strip())


def _agent_events(agent: object) -> list[float]:
    existing = getattr(agent, "_tool_agent_budget_events", None)
    if isinstance(existing, list):
        return existing
    created: list[float] = []
    agent._tool_agent_budget_events = created
    return created


def _unknown_command_events(agent: object) -> list[float]:
    existing = getattr(agent, "_unknown_command_budget_events", None)
    if isinstance(existing, list):
        return existing
    created: list[float] = []
    agent._unknown_command_budget_events = created
    return created


def _budget_int(
    config: object,
    key: str,
    *,
    policy: object = None,
    default: int = 0,
) -> int:
    if config is not None and hasattr(config, key):
        value = getattr(config, key, None)
        if value is None:
            return default
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return default
    if policy is not None and hasattr(policy, "int_value"):
        return policy.int_value(key, default)
    return max(0, runtime_guard_int(key, default))


def _budget_result(tool_name: str, max_calls: int, window_seconds: int) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        str(tool_name or "unknown"),
        False,
        (
            f"单个代理工具预算已达到：本代理最近 {window_seconds} 秒最多 {max_calls} 次工具调用。"
            "请不要继续重复请求新工具；先自检是否已经掌握足够事实。"
            "如果足够完成当前交付，请立刻用已有工具结果写入交付物并给出最终回复；"
            "只有确实缺少关键事实时，才简短说明具体缺口和需要继续调度、接管或提高预算的原因。"
        ),
        error_code="TOOL_RATE_LIMIT_EXCEEDED",
    )
