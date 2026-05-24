# LLM: Tool-loop response decisions stay separate from ToolLoopService control flow.
# 模块用途: 判断一次模型回复应该继续生成、结束，还是执行真实工具，并处理伪造工具回执。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._runtime_params import ToolLoopExecuteParams
from .tool_bootstrap_materialization_guard import (
    bootstrap_materialization_block_response,
    bootstrap_materialization_context,
    has_required_bootstrap_materialization,
    is_bootstrap_materialization_evidence_call,
    is_bootstrap_materialization_productive_call,
    should_redirect_bootstrap_evidence,
)
from .tool_delivery_repair_call_normalizer import normalize_delivery_repair_calls
from .tool_delivery_repair_guard import (
    delivery_repair_block_response,
    delivery_repair_context,
    delivery_repair_rejection_context,
    has_required_delivery_repair,
    is_delivery_repair_productive_call,
)
from .tool_local_progress_guard import (
    has_required_local_progress_guard,
    local_progress_guard_block_response,
    local_progress_guard_context,
)
from .tool_loop_exploration_decision import (
    ExplorationFuseDecision,
    ExplorationFuseDecisionRequest,
    exploration_fuse_no_tool_call_decision,
    exploration_fuse_tool_call_decision,
)
from .tool_loop_open_session_decision import (
    OpenSessionDecision,
    OpenSessionDecisionRequest,
    open_session_tool_call_decision,
    open_write_session_decision,
)
from .tool_loop_repair_counters import (
    ToolLoopRepairCounters,
    _inc_bootstrap_materialization,
    _inc_delivery_repair,
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
    open_session_tools = open_session_tool_call_decision(_open_session_request(request, calls))
    if open_session_tools is not None:
        return _open_session_decision(open_session_tools)
    calls = normalize_delivery_repair_calls(request.agent, calls, request.params)
    delivery_repair_active = has_required_delivery_repair(request.agent, request.params)
    delivery_repair_tools = _delivery_repair_tool_call_decision(request, calls)
    if delivery_repair_tools is not None:
        return delivery_repair_tools
    if delivery_repair_active:
        clean_response = sanitize_reserved_tool_record_response(request.response)
        return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)
    local_progress_tools = _local_progress_tool_call_decision(request, calls)
    if local_progress_tools is not None:
        return local_progress_tools
    bootstrap_decision = _bootstrap_materialization_tool_call_decision(request, calls)
    if bootstrap_decision is not None:
        return bootstrap_decision
    exploration_fuse = exploration_fuse_tool_call_decision(_exploration_request(request, calls))
    if exploration_fuse is not None:
        return _exploration_decision(exploration_fuse)
    clean_response = sanitize_reserved_tool_record_response(request.response)
    return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)


# LLM: _no_tool_calls_decision prevents spoof-only reserved records from becoming final answers.
# 函数用途: 无真实工具调用时，普通回复直接收口；伪造工具回执先纠偏一次，再重复就阻断。
def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    open_session_decision = open_write_session_decision(_open_session_request(request, []))
    if open_session_decision is not None:
        return _open_session_decision(open_session_decision)
    delivery_repair_decision = _delivery_repair_no_tool_call_decision(request)
    if delivery_repair_decision is not None:
        return delivery_repair_decision
    bootstrap_decision = _bootstrap_materialization_no_tool_call_decision(request)
    if bootstrap_decision is not None:
        return bootstrap_decision
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


# LLM: _delivery_repair_no_tool_call_decision prevents staged-repair tasks from ending or chatting while write-first recovery actions remain.
# 函数用途: closeout 已要求先修阶段产物时，若模型没有给任何工具调用，就先纠偏；连续忽略则阻断。
def _delivery_repair_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    if not has_required_delivery_repair(request.agent, request.params):
        return None
    repair_context = delivery_repair_context(
        request.agent,
        request.counters.delivery_repair_redirects,
        request.params,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_delivery_repair(request.counters))
    block = delivery_repair_block_response(request.agent, request.params)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _bootstrap_materialization_no_tool_call_decision stops the assistant from chatting before the first structured target exists.
# 函数用途: 开工阶段所有目标都还缺失时，如果模型没给工具调用，就先纠偏；重复忽略后阻断。
def _bootstrap_materialization_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    if not has_required_bootstrap_materialization(request.agent, request.params, []):
        return None
    repair_context = bootstrap_materialization_context(
        request.agent,
        request.params,
        request.counters.bootstrap_materialization_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_bootstrap_materialization(request.counters))
    block = bootstrap_materialization_block_response(request.agent, request.params)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _local_progress_no_tool_call_decision blocks empty chatter turns once the machine closeout report shows repeated exploration without new local work.
# 函数用途: 连续多轮没有任何本地推进时，普通文本回复也要先被拉回 checkpoint/draft/builder 主链，而不是继续聊天。
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
    block = local_progress_guard_block_response(request.agent)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _bootstrap_materialization_tool_call_decision permits evidence gathering while redirecting pure inspection startup turns.
# 函数用途: bootstrap 目标缺失时允许先抓取真实证据，但纯检查仍先纠偏；重复无进展达到阈值后阻断。
def _bootstrap_materialization_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    if not has_required_bootstrap_materialization(request.agent, request.params, calls):
        return None
    if is_bootstrap_materialization_productive_call(calls, _bootstrap_materialization_payload(request)):
        return None
    if is_bootstrap_materialization_evidence_call(calls):
        block = bootstrap_materialization_block_response(request.agent, request.params)
        if block is not None:
            return ToolLoopResponseDecision("break", block, [], request.counters)
        if should_redirect_bootstrap_evidence(request.agent):
            repair_context = bootstrap_materialization_context(
                request.agent,
                request.params,
                request.counters.bootstrap_materialization_redirects,
            )
            if repair_context:
                request.params.tool_context.append(repair_context)
                return ToolLoopResponseDecision("continue", None, [], _inc_bootstrap_materialization(request.counters))
        return None
    repair_context = bootstrap_materialization_context(
        request.agent,
        request.params,
        request.counters.bootstrap_materialization_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_bootstrap_materialization(request.counters))
    block = bootstrap_materialization_block_response(request.agent, request.params)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


def _bootstrap_materialization_payload(request: ToolLoopResponseDecisionRequest) -> dict[str, object]:
    from .tool_bootstrap_materialization_guard import _bootstrap_payload

    return _bootstrap_payload(request.agent, request.params)


# LLM: _local_progress_tool_call_decision redirects repeated remote/read-only exploration when closeout facts show the local workspace has stopped changing.
# 函数用途: 利用结构化 closeout 进展指纹判断“只抓不落地”的空转；先纠偏，连续忽略后再阻断。
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
    block = local_progress_guard_block_response(request.agent)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _delivery_repair_tool_call_decision redirects inspection-only tool calls while staged-delivery recovery still requires writes or builder actions.
# 函数用途: 当模型在必须先写/修/构建的阶段仍只发检查类工具调用时，先追加结构化纠偏合同，再重复忽略就阻断。
def _delivery_repair_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    if is_delivery_repair_productive_call(request.agent, calls, request.params):
        return None
    repair_context = delivery_repair_rejection_context(
        request.agent,
        calls,
        request.counters.delivery_repair_redirects,
        request.params,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_delivery_repair(request.counters))
    block = delivery_repair_block_response(request.agent, request.params)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


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


# LLM: _open_session_request adapts either tool-call or no-tool requests into the split module shape.
# 函数用途: 复用 open-session 分支的 request dataclass，避免主决策函数继续扩展参数。
def _open_session_request(
    request: ToolLoopResponseDecisionRequest | _NoToolCallsRequest,
    calls: list[dict[str, object]],
) -> OpenSessionDecisionRequest:
    return OpenSessionDecisionRequest(request.agent, request.params, request.response, request.counters, calls)


# LLM: _open_session_decision adapts the split open-session module into this module's public decision type.
# 函数用途: 保持 tool_loop_response_decision 对外返回类型不变。
def _open_session_decision(decision: OpenSessionDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)
