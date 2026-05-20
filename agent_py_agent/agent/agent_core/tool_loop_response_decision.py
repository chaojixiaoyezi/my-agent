# LLM: Tool-loop response decisions stay separate from ToolLoopService control flow.
# 模块用途: 判断一次模型回复应该继续生成、结束，还是执行真实工具，并处理伪造工具回执。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..tooling.file_write_session_inspection import open_file_write_sessions
from ._runtime_params import ToolLoopExecuteParams
from .tool_bootstrap_materialization_guard import (
    bootstrap_materialization_block_response,
    bootstrap_materialization_context,
    has_required_bootstrap_materialization,
    is_bootstrap_materialization_productive_call,
)
from .tool_delivery_repair_guard import (
    delivery_repair_block_response,
    delivery_repair_context,
    has_required_delivery_repair,
    is_delivery_repair_productive_call,
)
from .tool_local_progress_guard import (
    has_required_local_progress_guard,
    local_progress_guard_block_response,
    local_progress_guard_context,
)
from .tool_open_write_session_repair import (
    open_write_session_block_response,
    open_write_session_repair_context,
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


# LLM: ToolLoopRepairCounters keeps repair bookkeeping out of ToolLoopService positional params.
# 类用途: 保存工具循环里的纠偏计数，避免每新增一种修复都扩散方法签名。
@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    reserved_record_repairs: int = 0
    open_write_session_repairs: int = 0
    bootstrap_materialization_redirects: int = 0
    local_progress_redirects: int = 0
    delivery_repair_redirects: int = 0


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
        open_session_tools = _open_session_tool_call_decision(request, calls)
        if open_session_tools is not None:
            return open_session_tools
        bootstrap_decision = _bootstrap_materialization_tool_call_decision(request, calls)
        if bootstrap_decision is not None:
            return bootstrap_decision
        local_progress_tools = _local_progress_tool_call_decision(request, calls)
        if local_progress_tools is not None:
            return local_progress_tools
        delivery_repair_tools = _delivery_repair_tool_call_decision(request, calls)
        if delivery_repair_tools is not None:
            return delivery_repair_tools
        clean_response = sanitize_reserved_tool_record_response(request.response)
        return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)

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


# LLM: _no_tool_calls_decision prevents spoof-only reserved records from becoming final answers.
# 函数用途: 无真实工具调用时，普通回复直接收口；伪造工具回执先纠偏一次，再重复就阻断。
def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    open_session_decision = _open_write_session_decision(request)
    if open_session_decision is not None:
        return open_session_decision
    bootstrap_decision = _bootstrap_materialization_no_tool_call_decision(request)
    if bootstrap_decision is not None:
        return bootstrap_decision
    local_progress_decision = _local_progress_no_tool_call_decision(request)
    if local_progress_decision is not None:
        return local_progress_decision
    delivery_repair_decision = _delivery_repair_no_tool_call_decision(request)
    if delivery_repair_decision is not None:
        return delivery_repair_decision
    if not request.has_reserved_record:
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.reserved_record_repairs < 1:
        return ToolLoopResponseDecision("continue", None, [], _inc_reserved(request.counters))
    final = reserved_tool_record_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


# LLM: _open_write_session_decision blocks final prose while chunked writes remain open.
# 函数用途: 基于 manifest 事实判断是否需要继续 finish/abort 分块写入会话。
def _open_write_session_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    open_session_context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
    )
    if open_session_context:
        request.params.tool_context.append(open_session_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_open_session(request.counters))
    if request.counters.open_write_session_repairs:
        block = open_write_session_block_response(request.agent)
        if block is not None:
            return ToolLoopResponseDecision("break", block, [], request.counters)
    return None


# LLM: _open_session_tool_call_decision enforces that open staged writes must be continued or finished before new writes start.
# 函数用途: 只要有 open file_write_session，下一轮工具调用就只能继续对应 session；否则先纠偏，再重复就阻断。
def _open_session_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    sessions = open_file_write_sessions(_agent_root(request.agent))
    if not sessions:
        return None
    open_ids = {str(item.get("session_id") or "") for item in sessions if str(item.get("session_id") or "")}
    for call in calls:
        tool = str(call.get("tool") or "").strip()
        action = str(call.get("action") or "").strip().lower()
        session_id = str(call.get("session_id") or "").strip()
        if tool != "file_write_session":
            return _invalid_open_session_tool_call(request)
        if action not in {"append", "finish", "abort"}:
            return _invalid_open_session_tool_call(request)
        if session_id not in open_ids:
            return _invalid_open_session_tool_call(request)
    return None


# LLM: _invalid_open_session_tool_call turns ignored staged-write contracts into deterministic repair or block outcomes.
# 函数用途: 模型在 open session 存在时仍想开新写入链路，就先给一次修正机会，再继续就阻断。
def _invalid_open_session_tool_call(
    request: ToolLoopResponseDecisionRequest,
) -> ToolLoopResponseDecision:
    open_session_context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
    )
    if open_session_context:
        request.params.tool_context.append(open_session_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_open_session(request.counters))
    block = open_write_session_block_response(request.agent)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _delivery_repair_no_tool_call_decision prevents staged-repair tasks from ending or chatting while write-first recovery actions remain.
# 函数用途: closeout 已要求先修阶段产物时，若模型没有给任何工具调用，就先纠偏；连续忽略则阻断。
def _delivery_repair_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    if not has_required_delivery_repair(request.agent):
        return None
    repair_context = delivery_repair_context(request.agent, request.counters.delivery_repair_redirects)
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_delivery_repair(request.counters))
    block = delivery_repair_block_response(request.agent)
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


# LLM: _bootstrap_materialization_tool_call_decision redirects pure inspection or fetch-only startup turns until one target is materialized.
# 函数用途: 当 bootstrap 目标一个都没出现时，模型必须先做创建/写入动作；否则只给纠偏机会，不执行空转工具。
def _bootstrap_materialization_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    if not has_required_bootstrap_materialization(request.agent, request.params, calls):
        return None
    if is_bootstrap_materialization_productive_call(calls):
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
    if is_delivery_repair_productive_call(request.agent, calls):
        return None
    repair_context = delivery_repair_context(request.agent, request.counters.delivery_repair_redirects)
    if repair_context:
        request.params.tool_context.append(repair_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_delivery_repair(request.counters))
    block = delivery_repair_block_response(request.agent)
    return ToolLoopResponseDecision("break", block or request.response, [], request.counters)


# LLM: _agent_root keeps open-session checks tolerant of lightweight test harnesses.
# 函数用途: 从 agent 读取真实工作区根目录，供 open file_write_session 检查复用。
def _agent_root(agent: object):
    from pathlib import Path

    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).resolve()


# LLM: _inc_reserved returns a new counters bundle after fake-record repair.
# 函数用途: 增加 reserved record 纠偏计数，保持 dataclass 不可变。
def _inc_reserved(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs + 1,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
    )


# LLM: _inc_open_session returns a new counters bundle after open-session repair.
# 函数用途: 增加分块写入 session 纠偏计数，保持 dataclass 不可变。
def _inc_open_session(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs + 1,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
    )


# LLM: _inc_local_progress returns a new immutable counters bundle after one local-progress redirect.
# 函数用途: 累加“回到本地推进”的纠偏次数，避免模型连续忽略后无限继续。
def _inc_local_progress(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects + 1,
        delivery_repair_redirects=counters.delivery_repair_redirects,
    )


# LLM: _inc_bootstrap_materialization returns a new immutable counters bundle after one startup-materialization redirect.
# 函数用途: 累加 bootstrap 开工纠偏计数，避免“先物化一个目标”的提醒无限重复。
def _inc_bootstrap_materialization(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects + 1,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
    )


# LLM: _inc_delivery_repair returns a new immutable counters bundle after one staged-delivery redirect.
# 函数用途: 增加 delivery repair 纠偏计数，避免该类状态无限提醒不收口。
def _inc_delivery_repair(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects + 1,
    )
