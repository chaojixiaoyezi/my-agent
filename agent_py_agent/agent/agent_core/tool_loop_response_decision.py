# LLM: Tool-loop response decisions stay separate from ToolLoopService control flow.
# 模块用途: 判断一次模型回复应该继续生成、结束，还是执行真实工具，并处理伪造工具回执。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams
from .main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from .tool_local_progress_guard import (
    has_required_local_progress_guard,
    local_progress_guard_context,
)
from .tool_loop_exploration_decision import (
    ExplorationFuseDecision,
    ExplorationFuseDecisionRequest,
    exploration_fuse_no_tool_call_decision,
    exploration_fuse_tool_call_decision,
)
from .tool_loop_repair_counters import (
    ToolLoopRepairCounters,
    _inc_local_progress,
    _inc_reserved,
)
from .tool_loop_unresolved_runtime_issue_decision import (
    UnresolvedRuntimeIssueDecision,
    UnresolvedRuntimeIssueDecisionRequest,
    unresolved_runtime_issue_no_tool_call_decision,
)
from .tool_reserved_record_guard import (
    contains_reserved_tool_record,
    reserved_tool_record_block_response,
    reserved_tool_record_repair_context,
    sanitize_reserved_tool_record_response,
)


# LLM: ToolLoopResponseDecisionRequest bundles response parsing inputs for code-size guardrails.
# 类用途: 集中保存工具循环决策所需上下文，避免 helper 函数参数继续扩散。
@dataclass(frozen=True)
class ToolLoopResponseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters


# LLM: ToolLoopResponseDecision stores the next action after parsing one model response.
# 类用途: 返回工具循环下一步动作、可执行工具调用、清理后的 response 和纠偏计数。
@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


# LLM: _NoToolCallsRequest bundles final-answer checks so helper signatures stay stable.
# 类用途: 保存无工具调用时判断收口、纠偏或阻断需要的上下文字段。
@dataclass(frozen=True)
class _NoToolCallsRequest:
    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    has_reserved_record: bool


# LLM: tool_loop_response_decision centralizes fake-record repair and tool-call parsing.
# 函数用途: 根据模型回复决定继续生成、结束或执行真实工具；伪造系统回执只给一次纠偏机会。
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


# LLM: _disabled_tools_response blocks fake transcript markers even when tools are disabled.
# 函数用途: 工具关闭时普通回复直接返回；如果模型仍伪造工具回执，则返回确定性阻断。
def _disabled_tools_response(response, has_reserved_record: bool):
    if not has_reserved_record:
        return response
    return reserved_tool_record_block_response(response.backend)


# LLM: _tool_calls_decision applies pre-execution guards before real tools run.
# 函数用途: 让主入口保持薄层分发；open-session/bootstrap/local/delivery/exploration 都在真实执行前处理。
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


# LLM: _no_tool_calls_decision prevents spoof-only reserved records from becoming final answers.
# 函数用途: 无真实工具调用时，普通回复直接收口；伪造工具回执先纠偏一次，再重复就阻断。
def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    delivery_closeout_decision = _implicit_delivery_closeout_decision(request)
    if delivery_closeout_decision is not None:
        return delivery_closeout_decision
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


# LLM: no-tool final text is an implicit delivery submission when a delivery contract exists.
# 函数用途: 模型准备最终回答时先跑机器验收；通过才完成，失败则把返工单放回下一轮。
def _implicit_delivery_closeout_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    before_context_count = len(request.params.tool_context)
    already_had_rework_context = _has_delivery_rework_context(request.params)
    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(
            agent=request.agent,
            params=request.params,
            backend=request.response.backend,
        )
    )
    if response is not None:
        return ToolLoopResponseDecision("break", response, [], request.counters)
    if len(request.params.tool_context) > before_context_count:
        if already_had_rework_context:
            return ToolLoopResponseDecision(
                "break",
                _delivery_rework_still_required_response(request.response),
                [],
                request.counters,
            )
        return ToolLoopResponseDecision("continue", None, [], request.counters)
    return None


def _has_delivery_rework_context(params: ToolLoopExecuteParams) -> bool:
    return any(str(item).startswith("[delivery-contract-check]") for item in params.tool_context)


def _delivery_rework_still_required_response(response) -> ModelResponse:
    return ModelResponse(
        text=(
            "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]\n"
            "交付物仍未通过客观 closeout 检查；本轮没有新的修复工具调用，已停止继续空转。"
        ),
        backend=response.backend,
    )


# LLM: _local_progress_no_tool_call_decision gives a soft rework hint after repeated exploration without new local work.
# 函数用途: 连续多轮没有任何本地推进时，给模型补充 checkpoint/draft/builder 返工提示；没有新提示时不阻断。
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


# LLM: _local_progress_tool_call_decision redirects repeated remote/read-only exploration only on soft hint rounds.
# 函数用途: 利用结构化 closeout 进展指纹判断“只抓不落地”的空转；只给返工提示，不把任务终止。
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


# LLM: _exploration_decision adapts the split exploration module into this module's public decision type.
# 函数用途: 保持 tool_loop_response_decision 的返回类型稳定，同时让 exploration 分支独立演进。
def _exploration_decision(decision: ExplorationFuseDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_decision(decision: UnresolvedRuntimeIssueDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_request(
    request: _NoToolCallsRequest,
) -> UnresolvedRuntimeIssueDecisionRequest:
    return UnresolvedRuntimeIssueDecisionRequest(request.agent, request.params, request.response, request.counters)


# LLM: _exploration_request adapts either request shape into the exploration module contract.
# 函数用途: 统一 tool-call 和 no-tool 分支传入 exploration fuse 的字段。
def _exploration_request(
    request: ToolLoopResponseDecisionRequest | _NoToolCallsRequest,
    calls: list[dict[str, object]],
) -> ExplorationFuseDecisionRequest:
    return ExplorationFuseDecisionRequest(request.agent, request.params, request.response, request.counters, calls)
