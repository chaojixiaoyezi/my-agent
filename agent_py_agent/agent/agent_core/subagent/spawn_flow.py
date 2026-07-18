
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..spawn_role_seed import (
    SpawnExplicitRoleRequest,
    is_explicit_root_role,
    spawn_explicit_role_runs,
)
from .params import SpawnSubagentsParams


@dataclass(frozen=True)
class SpawnSubagentsFlowRequest:
    agent: Any
    options: SpawnSubagentsParams


def spawn_subagents_flow(request: SpawnSubagentsFlowRequest):
    if is_explicit_root_role(request.options.role, _role_template_dirs(request.agent)):
        return _spawn_explicit_root_seed(request)
    if request.options.count is None:
        return _spawn_default_count(request)
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


def _role_template_dirs(agent: object) -> object:
    subagents = getattr(agent, "subagents", None)
    return getattr(subagents, "role_template_dirs", None)


def effective_max_subagents(value: object, *, default: int) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return default
    return cap if cap > 0 else default


def _spawn_explicit_root_seed(request: SpawnSubagentsFlowRequest):
    count = min(
        request.options.count or 1,
        effective_max_subagents(request.agent.config.max_subagents, default=request.options.count or 1),
    )
    return spawn_explicit_role_runs(
        SpawnExplicitRoleRequest(
            agent=request.agent,
            options=request.options,
            count=count,
            allowed_tools=configured_subagent_allowed_tools(request.agent.config),
        )
    )


def _spawn_default_count(request: SpawnSubagentsFlowRequest):
    return _split_for_max_subagents(
        request,
        default_count=effective_max_subagents(
            getattr(request.agent.config, "subagent_spawn_default_count", 3),
            default=3,
        ),
        allowed_tools=configured_subagent_allowed_tools(request.agent.config),
    )


def _spawn_fixed_count(request: SpawnSubagentsFlowRequest):
    return _split_for_max_subagents(
        request,
        default_count=request.options.count or 1,
        allowed_tools=configured_subagent_allowed_tools(request.agent.config),
    )


def _split_for_max_subagents(
    request: SpawnSubagentsFlowRequest,
    *,
    default_count: int,
    allowed_tools: list[str] | None,
):
    count = default_count
    if request.options.count is not None:
        count = request.options.count
    max_subagents = effective_max_subagents(request.agent.config.max_subagents, default=0)
    if max_subagents > 0:
        count = min(count, max_subagents)
    return request.agent.subagents.split(
        request.options.goal,
        count,
        allowed_tools=allowed_tools,
    )
