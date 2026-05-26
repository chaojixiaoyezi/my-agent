# LLM: Open file-write-session decisions stay outside the central tool-loop parser.
# 模块用途: 处理分块写入 session 未结束时的继续/阻断逻辑，避免主 response decision 文件继续变胖。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..tooling.file_write_session_inspection import open_file_write_sessions
from .open_write_session_config import open_write_session_config
from .tool_loop_repair_counters import (
    ToolLoopRepairCounters,
    _inc_open_session,
    _reset_open_session,
)
from .tool_open_write_session_repair import open_write_session_repair_context


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


# LLM: open_write_session_decision refuses final prose while staged writes remain open.
# 函数用途: 最终回答是关键收口动作；有 open session 时只返回返工提示，不把任务置为 blocked。
def open_write_session_decision(request: OpenSessionDecisionRequest) -> OpenSessionDecision | None:
    context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
        request.params,
        reason="final_response_before_file_write_session_finish",
    )
    if context:
        request.params.tool_context.append(context)
        return OpenSessionDecision("run_tools", request.response, [], _inc_open_session(request.counters))
    return None


# LLM: open_session_tool_call_decision protects staged targets without freezing unrelated work.
# 函数用途: open session 存在时只拒绝收口、读写同目标等关键冲突；非冲突工具继续执行。
def open_session_tool_call_decision(request: OpenSessionDecisionRequest) -> OpenSessionDecision | None:
    sessions = _open_sessions_for_request(request)
    if not sessions:
        return None
    open_ids = {str(item.get("session_id") or "") for item in sessions if str(item.get("session_id") or "")}
    if _all_calls_handle_open_sessions(request.calls, open_ids):
        return OpenSessionDecision("run_tools", request.response, request.calls, _reset_open_session(request.counters))
    conflict_reason = _critical_conflict_reason(request, sessions, open_ids)
    if conflict_reason:
        return _open_session_reminder(request, conflict_reason, execute_calls=False)
    if _any_call_handles_open_session(request.calls, open_ids):
        return OpenSessionDecision("run_tools", request.response, request.calls, _reset_open_session(request.counters))
    reason = "periodic_open_session_reminder" if _periodic_reminder_due(request) else ""
    return _open_session_reminder(request, reason, execute_calls=True)


# LLM: _open_session_reminder appends structured facts and optionally still executes non-conflicting tools.
# 函数用途: 把 open session 情况写回下一轮 prompt；关键冲突不执行，普通周期提醒继续执行当前工具。
def _open_session_reminder(
    request: OpenSessionDecisionRequest,
    reason: str,
    *,
    execute_calls: bool,
) -> OpenSessionDecision:
    context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
        request.params,
        reason=reason,
    )
    if context and reason:
        request.params.tool_context.append(context)
    counters = _inc_open_session(request.counters)
    calls = request.calls if execute_calls else []
    return OpenSessionDecision("run_tools", request.response, calls, counters)


# LLM: _all_calls_handle_open_sessions detects real work on the currently open file sessions.
# 函数用途: 只要本轮明确 append/finish/reset/abort 当前 session，就清零未处理回合计数。
def _all_calls_handle_open_sessions(calls: list[dict[str, object]], open_ids: set[str]) -> bool:
    if not calls:
        return False
    for call in calls:
        if not _call_handles_open_session(call, open_ids):
            return False
    return True


# LLM: _any_call_handles_open_session lets mixed safe turns clear the ignored-turn counter.
# 函数用途: 一轮里只要处理了当前 session，就不把同轮其他非冲突工具算成忽略。
def _any_call_handles_open_session(calls: list[dict[str, object]], open_ids: set[str]) -> bool:
    return any(_call_handles_open_session(call, open_ids) for call in calls)


def _call_handles_open_session(call: dict[str, object], open_ids: set[str]) -> bool:
    tool = str(call.get("tool") or "").strip()
    action = str(call.get("action") or "").strip().lower()
    session_id = str(call.get("session_id") or "").strip()
    return tool == "file_write_session" and action in {"append", "finish", "reset", "abort"} and session_id in open_ids


# LLM: _critical_conflict_reason checks only machine paths and tool names, never task prose.
# 函数用途: 判断当前工具调用是否会假收口、读旧目标或覆盖未提交目标。
def _critical_conflict_reason(
    request: OpenSessionDecisionRequest,
    sessions: list[dict[str, object]],
    open_ids: set[str],
) -> str:
    open_targets = _open_target_paths(request.agent, sessions)
    for call in request.calls:
        if _is_acceptance_submit(call):
            return "submit_for_acceptance_before_file_write_session_finish"
        if reason := _file_session_conflict_reason(request.agent, call, open_ids, open_targets):
            return reason
        if _is_reading_open_target(request.agent, call, open_targets):
            return "read_target_before_file_write_session_finish"
        if _is_writing_open_target(request.agent, call, open_targets):
            return "write_target_before_file_write_session_finish"
    return ""


def _is_acceptance_submit(call: dict[str, object]) -> bool:
    return str(call.get("tool") or "").strip() == "submit_for_acceptance"


def _file_session_conflict_reason(
    agent: object,
    call: dict[str, object],
    open_ids: set[str],
    open_targets: set[str],
) -> str:
    if str(call.get("tool") or "").strip() != "file_write_session":
        return ""
    action = str(call.get("action") or "").strip().lower()
    session_id = str(call.get("session_id") or "").strip()
    if action in {"append", "finish", "reset", "abort"} and session_id and session_id not in open_ids:
        return "file_write_session_action_for_unknown_open_session"
    if action == "begin" and _call_targets_open_path(agent, call, open_targets):
        return "new_file_write_session_for_existing_open_target"
    return ""


def _is_reading_open_target(agent: object, call: dict[str, object], open_targets: set[str]) -> bool:
    return str(call.get("tool") or "").strip() == "read_file" and _call_targets_open_path(
        agent, call, open_targets
    )


def _is_writing_open_target(agent: object, call: dict[str, object], open_targets: set[str]) -> bool:
    tool = str(call.get("tool") or "").strip()
    return tool in {"write_file", "append_file", "replace_in_file"} and _call_targets_open_path(
        agent, call, open_targets
    )


# LLM: _periodic_reminder_due counts model turns that left an open session unresolved.
# 函数用途: 非冲突工具允许执行；只按配置周期把 open session 事实再次提示给模型。
def _periodic_reminder_due(request: OpenSessionDecisionRequest) -> bool:
    interval = open_write_session_config(request.agent).reminder_interval
    next_count = request.counters.open_write_session_repairs + 1
    return interval > 0 and next_count % interval == 0


# LLM: _open_target_paths normalizes manifest target paths into comparable raw/display/resolved strings.
# 函数用途: 用机器 manifest 判断路径冲突，不读目标内容、不看自然语言。
def _open_target_paths(agent: object, sessions: list[dict[str, object]]) -> set[str]:
    root = _agent_root(agent)
    targets: set[str] = set()
    for session in sessions:
        targets.update(_target_path_forms(root, session.get("target_path")))
    return targets


def _target_path_forms(root: Path, value: object) -> set[str]:
    if not isinstance(value, dict):
        return _path_forms(root, value)
    forms: set[str] = set()
    for key in ("raw", "display", "resolved"):
        forms.update(_path_forms(root, value.get(key)))
    return forms


def _call_targets_open_path(agent: object, call: dict[str, object], open_targets: set[str]) -> bool:
    root = _agent_root(agent)
    for key in ("path", "target_path", "file_path", "artifact_path"):
        if _path_forms(root, call.get(key)) & open_targets:
            return True
    return False


def _path_forms(root: Path, value: object) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    forms = {text}
    try:
        path = Path(text).expanduser()
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    except (OSError, RuntimeError):
        return forms
    forms.add(str(resolved))
    try:
        forms.add(str(resolved.relative_to(root)))
    except ValueError:
        pass
    return forms


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
