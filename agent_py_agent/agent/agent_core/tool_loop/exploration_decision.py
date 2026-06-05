
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..tool_guard.exploration_fuse import (
    exploration_fuse_context,
    has_pending_exploration_fuse,
    has_required_exploration_fuse,
)
from .repair_counters import ToolLoopRepairCounters, _inc_exploration_fuse


@dataclass(frozen=True)
class ExplorationFuseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class ExplorationFuseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters
    calls: list[dict[str, object]]


def exploration_fuse_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_required_exploration_fuse(request.agent, request.calls):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects)
    if context:
        request.params.tool_context.append(context)
        return None
    return None


def exploration_fuse_no_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_pending_exploration_fuse(request.agent):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects)
    if context:
        request.params.tool_context.append(context)
        return ExplorationFuseDecision("continue", None, [], _inc_exploration_fuse(request.counters))
    return None


__all__ = [
    "ExplorationFuseDecision",
    "ExplorationFuseDecisionRequest",
    "exploration_fuse_no_tool_call_decision",
    "exploration_fuse_tool_call_decision",
]
