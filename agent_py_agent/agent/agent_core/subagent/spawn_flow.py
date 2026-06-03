
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..automation_guard import SubagentAutomationGuard
from ..spawn_role_seed import (
    SpawnExplicitRoleRequest,
    is_explicit_root_role,
    spawn_explicit_role_runs,
)
from ..task_complexity import estimate_task_complexity
from .params import SpawnSubagentsParams


@dataclass(frozen=True)
class SpawnSubagentsFlowRequest:
    agent: Any
    options: SpawnSubagentsParams
    workflow_mode: str


def spawn_subagents_flow(request: SpawnSubagentsFlowRequest):
    if is_explicit_root_role(request.options.role):
        return _spawn_explicit_root_seed(request)
    if request.options.count is None:
        return _spawn_auto_delegated(request)
    return _spawn_fixed_count(request)


def configured_subagent_allowed_tools(config: object) -> list[str] | None:
    value = getattr(config, "subagent_allowed_tools", [])
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return None
    tools = [str(item).strip() for item in raw_items if item is not None and str(item).strip()]
    return tools or None


def effective_max_subagents(value: object, *, fallback: int) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return fallback
    return cap if cap > 0 else fallback


def _spawn_explicit_root_seed(request: SpawnSubagentsFlowRequest):
    count = min(
        request.options.count or 1,
        effective_max_subagents(request.agent.config.max_subagents, fallback=request.options.count or 1),
    )
    return spawn_explicit_role_runs(
        SpawnExplicitRoleRequest(
            agent=request.agent,
            options=request.options,
            count=count,
            allowed_tools=configured_subagent_allowed_tools(request.agent.config),
            workflow_mode=request.workflow_mode,
        )
    )


def _spawn_auto_delegated(request: SpawnSubagentsFlowRequest):
    allowed_tools = configured_subagent_allowed_tools(request.agent.config)
    complexity = estimate_task_complexity(
        request.options.goal,
        plan=[],
        allowed_tools=allowed_tools or [],
    )
    guard = SubagentAutomationGuard(request.agent.config)
    should_delegate = guard.should_delegate(complexity)
    tasks = _split_for_max_subagents(request, fallback=1000, allowed_tools=allowed_tools) if should_delegate else []
    guard.warn_if_not_delegating(complexity, bool(tasks))
    return tasks


def _spawn_fixed_count(request: SpawnSubagentsFlowRequest):
    return _split_for_max_subagents(
        request,
        fallback=request.options.count or 1,
        allowed_tools=configured_subagent_allowed_tools(request.agent.config),
    )


def _split_for_max_subagents(
    request: SpawnSubagentsFlowRequest,
    *,
    fallback: int,
    allowed_tools: list[str] | None,
):
    count = effective_max_subagents(request.agent.config.max_subagents, fallback=fallback)
    if request.options.count is not None:
        count = min(request.options.count, count)
    return request.agent.subagents.split(
        request.options.goal,
        count,
        workflow_mode=request.workflow_mode,
        allowed_tools=allowed_tools,
    )
