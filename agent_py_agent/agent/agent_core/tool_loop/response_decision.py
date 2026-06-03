
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..tool_guard.local_progress import (
    has_required_local_progress_guard,
    local_progress_guard_context,
)
from ..tool_guard.reserved_record import (
    contains_reserved_tool_record,
    reserved_tool_record_block_response,
    reserved_tool_record_repair_context,
    sanitize_reserved_tool_record_response,
)
from .exploration_decision import (
    ExplorationFuseDecision,
    ExplorationFuseDecisionRequest,
    exploration_fuse_no_tool_call_decision,
    exploration_fuse_tool_call_decision,
)
from .repair_counters import (
    ToolLoopRepairCounters,
    _inc_local_progress,
    _inc_reserved,
)
from .unresolved_runtime_issue_decision import (
    UnresolvedRuntimeIssueDecision,
    UnresolvedRuntimeIssueDecisionRequest,
    unresolved_runtime_issue_no_tool_call_decision,
)


@dataclass(frozen=True)
class ToolLoopResponseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class _NoToolCallsRequest:
    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    has_reserved_record: bool


def tool_loop_response_decision(
    request: ToolLoopResponseDecisionRequest,
) -> ToolLoopResponseDecision:
    has_reserved_record = contains_reserved_tool_record(request.response.text)
    if has_reserved_record:
        request.params.tool_context.append(reserved_tool_record_repair_context())

    if not request.agent.config.enable_tools:
        final = _disabled_tools_response(request.response, has_reserved_record)
        return ToolLoopResponseDecision("break", final, [], request.counters)

    calls = request.agent.tools.parse_tool_calls(request.response.text)
    if calls:
        return _tool_calls_decision(request, calls)

    return _no_tool_calls_decision(
        _NoToolCallsRequest(
            request.agent,
            request.params,
            request.response,
            request.counters,
            has_reserved_record,
        )
    )


def _disabled_tools_response(response, has_reserved_record: bool):
    if not has_reserved_record:
        return response
    return reserved_tool_record_block_response(response.backend)


def _tool_calls_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision:
    local_progress_tools = _local_progress_tool_call_decision(request, calls)
    if local_progress_tools is not None:
        return local_progress_tools
    exploration_fuse = exploration_fuse_tool_call_decision(_exploration_request(request, calls))
    if exploration_fuse is not None:
        return _exploration_decision(exploration_fuse)
    clean_response = sanitize_reserved_tool_record_response(request.response)
    return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)


def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    local_progress_decision = _local_progress_no_tool_call_decision(request)
    if local_progress_decision is not None:
        return local_progress_decision
    unresolved_issue_decision = unresolved_runtime_issue_no_tool_call_decision(
        _unresolved_runtime_issue_request(request)
    )
    if unresolved_issue_decision is not None:
        return _unresolved_runtime_issue_decision(unresolved_issue_decision)
    exploration_fuse_decision = exploration_fuse_no_tool_call_decision(_exploration_request(request, []))
    if exploration_fuse_decision is not None:
        return _exploration_decision(exploration_fuse_decision)
    if not request.has_reserved_record:
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.reserved_record_repairs < 1:
        return ToolLoopResponseDecision("continue", None, [], _inc_reserved(request.counters))
    final = reserved_tool_record_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


def _local_progress_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    if not has_required_local_progress_guard(request.agent, request.params, []):
        return None
    repair_context = local_progress_guard_context(
        request.agent,
        request.counters.local_progress_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_local_progress(request.counters))
    return None


def _local_progress_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    if not has_required_local_progress_guard(request.agent, request.params, calls):
        return None
    repair_context = local_progress_guard_context(
        request.agent,
        request.counters.local_progress_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_local_progress(request.counters))
    return None


def _exploration_decision(decision: ExplorationFuseDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_decision(decision: UnresolvedRuntimeIssueDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_request(
    request: _NoToolCallsRequest,
) -> UnresolvedRuntimeIssueDecisionRequest:
    return UnresolvedRuntimeIssueDecisionRequest(request.agent, request.params, request.response, request.counters)


def _exploration_request(
    request: ToolLoopResponseDecisionRequest | _NoToolCallsRequest,
    calls: list[dict[str, object]],
) -> ExplorationFuseDecisionRequest:
    return ExplorationFuseDecisionRequest(request.agent, request.params, request.response, request.counters, calls)
