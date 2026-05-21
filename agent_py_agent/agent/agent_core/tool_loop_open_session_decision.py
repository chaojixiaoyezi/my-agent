# LLM: Open file-write-session decisions stay outside the central tool-loop parser.
# 模块用途: 处理分块写入 session 未结束时的继续/阻断逻辑，避免主 response decision 文件继续变胖。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..tooling.file_write_session_inspection import open_file_write_sessions
from .tool_loop_repair_counters import ToolLoopRepairCounters, _inc_open_session
from .tool_open_write_session_repair import (
    open_write_session_block_response,
    open_write_session_repair_context,
)


# LLM: OpenSessionDecision mirrors the central decision fields without circular imports.
# 类用途: 返回 action/response/calls/counters，供主决策层适配成公开 ToolLoopResponseDecision。
@dataclass(frozen=True)
class OpenSessionDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


# LLM: OpenSessionDecisionRequest bundles the facts needed by open-session guards.
# 类用途: 保存 agent、params、response、counters 和当前工具调用，避免 helper 参数膨胀。
@dataclass(frozen=True)
class OpenSessionDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters
    calls: list[dict[str, object]]


# LLM: open_write_session_decision blocks final prose while staged writes remain open.
# 函数用途: 基于 manifest 事实判断是否需要继续 finish/abort 分块写入会话。
def open_write_session_decision(request: OpenSessionDecisionRequest) -> OpenSessionDecision | None:
    context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
        request.params,
    )
    if context:
        request.params.tool_context.append(context)
        return OpenSessionDecision("continue", None, [], _inc_open_session(request.counters))
    if request.counters.open_write_session_repairs:
        block = open_write_session_block_response(request.agent, request.params)
        if block is not None:
            return OpenSessionDecision("break", block, [], request.counters)
    return None


# LLM: open_session_tool_call_decision enforces continuation of existing staged writes.
# 函数用途: open session 存在时，下一轮工具调用只能继续/finish/abort 对应 session。
def open_session_tool_call_decision(request: OpenSessionDecisionRequest) -> OpenSessionDecision | None:
    sessions = _open_sessions_for_request(request)
    if not sessions:
        return None
    open_ids = {str(item.get("session_id") or "") for item in sessions if str(item.get("session_id") or "")}
    for call in request.calls:
        tool = str(call.get("tool") or "").strip()
        action = str(call.get("action") or "").strip().lower()
        session_id = str(call.get("session_id") or "").strip()
        if tool != "file_write_session" or action not in {"append", "finish", "reset", "abort"} or session_id not in open_ids:
            return _invalid_open_session_tool_call(request)
    return None


# LLM: _invalid_open_session_tool_call converts ignored staged-write contracts into repair or block.
# 函数用途: 模型在 open session 存在时仍想开新写入链路，就先纠偏，再继续就阻断。
def _invalid_open_session_tool_call(request: OpenSessionDecisionRequest) -> OpenSessionDecision:
    context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
        request.params,
    )
    if context:
        request.params.tool_context.append(context)
        return OpenSessionDecision("continue", None, [], _inc_open_session(request.counters))
    block = open_write_session_block_response(request.agent, request.params)
    return OpenSessionDecision("break", block or request.response, [], request.counters)


# LLM: _agent_root keeps open-session checks tolerant of lightweight tests.
# 函数用途: 从 agent 读取真实工作区根目录，供 open file_write_session 检查复用。
def _agent_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).resolve()


# LLM: _open_sessions_for_request scopes open write repairs to the active machine request ids.
# 函数用途: 避免旧 run 崩溃留下的 open session 阻断后续独立主代理任务。
def _open_sessions_for_request(request: OpenSessionDecisionRequest) -> list[dict[str, object]]:
    params = request.params
    return open_file_write_sessions(
        _agent_root(request.agent),
        scope={
            "request_id": str(getattr(params, "request_id", "") or ""),
            "run_id": str(getattr(params, "run_id", "") or ""),
            "task_id": str(getattr(params, "task_id", "") or ""),
        },
    )


__all__ = [
    "OpenSessionDecision",
    "OpenSessionDecisionRequest",
    "open_session_tool_call_decision",
    "open_write_session_decision",
]
