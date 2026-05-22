# LLM: Unresolved-runtime-issue response decisions stay out of the central tool-loop file.
# 模块用途: 将“工具结构化失败未修复时不能最终回复”的分支独立出来，保持主决策层轻薄。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .tool_loop_repair_counters import ToolLoopRepairCounters, _inc_unresolved_runtime_issue
from .tool_unresolved_runtime_issue_guard import (
    has_unresolved_runtime_issues,
    unresolved_runtime_issue_block_response,
    unresolved_runtime_issue_context,
)


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters


def unresolved_runtime_issue_no_tool_call_decision(
    request: UnresolvedRuntimeIssueDecisionRequest,
) -> UnresolvedRuntimeIssueDecision | None:
    if not has_unresolved_runtime_issues(request.params):
        return None
    repair_context = unresolved_runtime_issue_context(
        request.params,
        request.counters.unresolved_runtime_issue_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return UnresolvedRuntimeIssueDecision(
            "continue",
            None,
            [],
            _inc_unresolved_runtime_issue(request.counters),
        )
    block = unresolved_runtime_issue_block_response(request.agent, request.params)
    return UnresolvedRuntimeIssueDecision("break", block or request.response, [], request.counters)


__all__ = [
    "UnresolvedRuntimeIssueDecision",
    "UnresolvedRuntimeIssueDecisionRequest",
    "unresolved_runtime_issue_no_tool_call_decision",
]
