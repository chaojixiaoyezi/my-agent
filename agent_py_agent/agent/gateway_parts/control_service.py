from __future__ import annotations

"""Conversation task controls backed by request and durable task ledgers.

给人看的解释：
飞书或终端发来的控制命令在这里查找“这个用户、这个会话”正在运行的任务。
短请求结束后仍沿着持久任务链接工作，只允许纠偏和停止自己的当前任务。
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..concurrency.interrupt import interrupt_by_name, is_interruptible_registered
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    ConversationTaskStatus,
    NamedConversationWorkStatus,
    conversation_request_interrupt_name,
    render_conversation_task_status,
    render_verbose_control,
)
from ..conversation.models import (
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    thread_task_run_started_at,
)
from .audit_control_service import AuditControlRequest, execute_audit_control_operation
from .goal_control_service import GoalControlRequest, execute_goal_control_operation
from .io import read_json_file_report, update_json_file_atomic
from .paths import GatewayPaths, gateway_chunk_path

_ACTIVE_SUBAGENT_STATUSES = {
    "PLANNING",
    "PENDING",
    "RUNNING",
    "BLOCKED",
    "PAUSED",
}
_DONE_SUBAGENT_STATUSES = {"DONE", "CANCELLED"}


@dataclass(frozen=True)
class GatewayControlScope:
    user_id: str
    channel: str
    conversation_id: str
    metadata: dict[str, object] = field(default_factory=dict)
    all_user_access: bool = False


@dataclass(frozen=True)
class _GatewayRequestRecord:
    path: Path | None
    payload: dict[str, object]
    target_kind: str = "request"
    linked_request: _GatewayRequestRecord | None = None


# LLM: Every adapter reaches the same typed control service; no IM-specific prompt branch is allowed.
# 函数用途：执行一条已解析的会话控制命令，并返回可直接回复用户的结果。
def execute_gateway_conversation_control(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(command.kind, False, command.usage)
    if command.kind == "goal":
        return _execute_goal_control(agent, command, scope)
    if command.kind == "audit":
        return _execute_audit_control(agent, paths, command, scope)
    if command.kind == "verbose":
        return _execute_verbose_control(agent, command, scope)
    if command.kind == "stop":
        live_request = _live_window_request(paths, scope)
        if live_request is not None:
            return _stop_live_window_request(agent, live_request, scope)
        live_task = _active_conversation_task(agent, scope, live_only=True)
        if live_task is not None:
            return _stop_active_task(agent, live_task, scope)
        return ConversationControlResult(
            "stop",
            False,
            "当前没有运行中的内容，无需停止。",
        )
    active = _active_control_target(agent, paths, scope)
    if command.kind == "status":
        status = _gateway_task_status(agent, paths, scope, active)
        return ConversationControlResult(
            "status",
            True,
            render_conversation_task_status(status),
            request_id=_record_id(active),
            status=status,
        )
    if active is None:
        message = (
            "当前没有运行中的内容，补充要求未保存。"
            if command.kind == "steer"
            else "当前没有运行中的内容，无需停止。"
        )
        return ConversationControlResult(
            command.kind,
            False,
            message,
        )
    if command.kind == "steer":
        return _steer_active_request(agent, active, command, scope)
    return _stop_active_request(agent, active, scope)


def _execute_audit_control(
    base_agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    """Resolve owner/thread authority, then delegate exact Audit lifecycle semantics."""
    try:
        owner_agent = _request_agent(base_agent, _scope_request_payload(scope))
        store = owner_agent.conversation_store
        pending = (
            _pending_exact_audit_requests(paths, scope, command.name)
            if command.operation == "clear"
            else []
        )
        stopped_request_ids: list[str] = []
        for record in pending:
            request_id = _record_id(record)
            marked, _failure = _mark_request_stopping(record, scope)
            if not marked:
                continue
            stopped_request_ids.append(request_id)
            interrupt_by_name(conversation_request_interrupt_name(request_id))
            _cancel_request_subagents_async(base_agent, record.payload, request_id)
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""
                ),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "Audit",
            }
        )
        result = execute_audit_control_operation(
            AuditControlRequest(
                owner_agent=owner_agent,
                store=store,
                thread=thread,
                command=command,
            )
        )
        if (
            not result.ok
            and command.operation == "clear"
            and stopped_request_ids
            and result.message == "没有找到这个 Audit。"
        ):
            return ConversationControlResult(
                "audit",
                True,
                f"Audit“{command.name}”已停止。",
                request_id=stopped_request_ids[0],
            )
        return result
    except Exception:
        return ConversationControlResult("audit", False, "Audit 状态暂时不可用，请稍后重试。")


def _pending_exact_audit_requests(
    paths: GatewayPaths,
    scope: GatewayControlScope,
    name: str,
) -> list[_GatewayRequestRecord]:
    selected: list[_GatewayRequestRecord] = []
    for folder in (paths.processing, paths.inbox):
        for record in _matching_requests(folder, scope):
            system_task = record.payload.get("system_task")
            attributes = (
                system_task.get("attributes")
                if isinstance(system_task, dict)
                and str(system_task.get("kind") or "") in {"audit", "audit_prepare"}
                else None
            )
            if (
                isinstance(attributes, dict)
                and (
                    not name
                    or str(attributes.get("conversation_work_name") or "") == name
                )
                and not bool(record.payload.get("cancel_requested"))
            ):
                selected.append(record)
    return selected


# LLM: Ordinary input during one live conversation follows the same structured active-turn
# steer path as explicit /btw; callers must not classify the message text to choose this path.
# 函数用途：若当前 owner/thread 正在执行，把一条普通用户输入写入该轮；没有活跃轮则返回 None。
def steer_active_conversation_if_running(
    agent: object,
    paths: GatewayPaths,
    *,
    message: str,
    scope: GatewayControlScope,
) -> ConversationControlResult | None:
    """Route ordinary input into the exact active turn, or report no active turn.

    This adapts 会话运行时 ``steer_input`` semantics to the durable Gateway thread:
    structured owner/conversation state selects the exact live run, while the
    message remains opaque user input.  A caller can safely fall back to a normal
    queued request when this returns ``None`` or a non-success result.
    """
    # Ordinary input joins only a currently executing Gateway turn.  A durable
    # task link without a live request is not enough: in that state a fresh
    # ordinary turn must be queued so it owns a real reply envelope.  Explicit
    # /btw remains able to steer that durable task through
    # execute_gateway_conversation_control().
    processing = _active_request(paths, scope)
    if processing is None:
        return None
    linked_task_id = _linked_conversation_task_id(processing)
    active = (
        _linked_active_task_record(agent, processing, scope, linked_task_id)
        if linked_task_id
        else None
    ) or processing
    user_input = str(message or "").strip()
    result = _steer_active_request(
        agent,
        active,
        ConversationControlCommand("steer", value=user_input, valid=bool(user_input)),
        scope,
    )
    if result.ok and active is not processing:
        # `/ask` callers keep polling the live Gateway request that already owns
        # the response stream.  The guidance itself remains bound to the linked
        # durable task so it survives the foreground/background handoff.
        return ConversationControlResult(
            result.kind,
            result.ok,
            result.message,
            request_id=_record_id(processing),
            status=result.status,
        )
    return result


# LLM: Resolve authority from trusted scope, then delegate lifecycle semantics to the single goal service.
# 函数用途: 为显式 `/goal` 命令解析当前 owner/thread 并执行目标操作。
def _execute_goal_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    """Execute an explicit `/goal` lifecycle operation on this exact conversation."""
    try:
        owner_agent = _request_agent(base_agent, _scope_request_payload(scope))
        store = owner_agent.conversation_store
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": command.value[:80] or "持续目标",
            }
        )
        return execute_goal_control_operation(
            GoalControlRequest(
                owner_agent=owner_agent,
                store=store,
                thread=thread,
                command=command,
                scope=scope,
                interrupt_goal=lambda goal: _interrupt_goal_task(owner_agent, goal),
                resume_registry=lambda task_id: _resume_task_registry_record(owner_agent, task_id),
            )
        )
    except Exception:
        return ConversationControlResult("goal", False, "持续目标状态暂时不可用，请稍后重试。")


# LLM: Verbose is a per-thread control setting; the command text never enters a model turn.
# 函数用途：在精确 owner/thread 上读取或修改过程显示档位，并返回确定性系统回执。
def _execute_verbose_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    try:
        owner_agent = _request_agent(base_agent, _scope_request_payload(scope))
        store = owner_agent.conversation_store
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""
                ),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "会话设置",
            }
        )
        if command.value:
            thread = store.update_verbose_level(thread.thread_id, command.value)
        level = str(getattr(thread, "verbose_level", "off") or "off")
        return ConversationControlResult(
            "verbose",
            True,
            render_verbose_control(level, command),
        )
    except Exception:
        return ConversationControlResult(
            "verbose",
            False,
            "当前会话的详细过程设置暂时不可用，请稍后重试。",
        )


# LLM: Pause/clear interrupts the current goal turn and descendants while preserving its durable task workspace.
# 函数用途: 停止持续目标当前执行域与子代理，但不删除目标现场。
def _interrupt_goal_task(owner_agent: object, goal: object) -> None:
    task_id = str(getattr(goal, "task_id", "") or "")
    store = owner_agent.conversation_store
    store.update_task_status({"task_id": task_id, "status": "interrupted"})
    _interrupt_task_registry_record(owner_agent, task_id)
    interrupt_by_name(conversation_request_interrupt_name(task_id))
    payload = {"id": task_id, "request_id": task_id}
    _cancel_request_subagents_async(owner_agent, payload, task_id)


# LLM: Resume updates the existing task projection; it never registers a second task identity.
# 函数用途: 恢复原持续任务在全局任务索引中的 running 状态。
def _resume_task_registry_record(owner_agent: object, task_id: str) -> None:
    try:
        registry = owner_agent.local_store.task_registry
        current = registry.lookup_task(task_id)
        if current is None:
            return
        registry.register_task(
            task_id,
            status="running",
            goal=str(current.get("goal") or ""),
            session_id=str(current.get("session_id") or ""),
            user_id=str(current.get("user_id") or ""),
        )
    except Exception:
        return


# LLM: Durable task selection wins over a transient chat request and uses only owner/thread links.
# 函数用途：先找会话仍活跃的根任务；尚未晋升时才回落 processing 请求。
def _active_control_target(
    base_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> _GatewayRequestRecord | None:
    processing = _active_request(paths, scope)
    linked_task_id = _linked_conversation_task_id(processing)
    if linked_task_id:
        durable = _linked_active_task_record(base_agent, processing, scope, linked_task_id)
        if durable is not None:
            return durable
        # The durable task may have crossed to interrupted while its live request
        # is still draining.  Keep controls on that exact request instead of
        # jumping to an unrelated stale task in the same conversation.
        return processing
    return _active_conversation_task(base_agent, scope) or processing


def _linked_active_task_record(
    base_agent: object,
    processing: _GatewayRequestRecord,
    scope: GatewayControlScope,
    task_id: str,
) -> _GatewayRequestRecord | None:
    """Bind one live request only to the durable task named by its runtime facts."""
    durable = _active_conversation_task(base_agent, scope, task_id=task_id)
    if durable is None:
        return None
    return _GatewayRequestRecord(
        durable.path,
        durable.payload,
        durable.target_kind,
        processing,
    )


# LLM: Request selection uses structured owner/channel/conversation facts and never message text.
# 函数用途：找到当前会话唯一的 processing 请求；同会话单飞时通常只有一个。
def _active_request(paths: GatewayPaths, scope: GatewayControlScope) -> _GatewayRequestRecord | None:
    records = [
        record
        for record in _matching_requests(paths.processing, scope)
        if not _request_is_detached(record)
    ]
    if not records:
        return None
    return max(records, key=lambda item: _request_timestamp(item.payload))


def _live_window_request(
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> _GatewayRequestRecord | None:
    """Return the exact live model turn, including a detached task's ingress turn."""
    records = list(_matching_requests(paths.processing, scope))
    if not records:
        return None
    return max(records, key=lambda item: _request_timestamp(item.payload))


def _request_is_detached(record: _GatewayRequestRecord) -> bool:
    if str(record.payload.get("conversation_cancellation_scope") or "") == "detached":
        return True
    system_task = record.payload.get("system_task")
    if not isinstance(system_task, dict):
        return False
    if str(system_task.get("kind") or "") == "audit":
        return True
    attributes = system_task.get("attributes")
    return bool(
        isinstance(attributes, dict)
        and str(attributes.get("conversation_cancellation_scope") or "") == "detached"
    )


def _active_conversation_task(
    base_agent: object,
    scope: GatewayControlScope,
    *,
    task_id: str = "",
    live_only: bool = False,
) -> _GatewayRequestRecord | None:
    """Resolve the latest user-selectable active root task in this exact owner conversation."""
    scope_payload = _scope_request_payload(scope)
    try:
        owner_agent = _request_agent(base_agent, scope_payload)
        store = owner_agent.conversation_store
        thread = store.resolve_thread(
            channel=scope.channel,
            channel_conversation_id=scope.conversation_id,
            channel_user_id=scope.user_id,
        )
        if thread is None:
            return None
        links, load_errors = store.task_links_report(thread.thread_id)
    except Exception:
        return None
    if load_errors:
        return None
    active = _control_active_conversation_links(store, thread.thread_id, links)
    if live_only:
        active = [
            link
            for link in active
            if _conversation_link_has_live_executor(
                owner_agent,
                store,
                thread.thread_id,
                link,
            )
        ]
    if not active:
        return None
    selected = _select_active_conversation_link(active, task_id)
    if selected is None:
        return None
    task_id = str(getattr(selected, "task_id", "") or "").strip()
    created_at = float(getattr(selected, "created_at", 0.0) or 0.0)
    payload = {
        **scope_payload,
        "id": task_id,
        "request_id": task_id,
        "goal": str(getattr(selected, "goal", "") or ""),
        "created_at": created_at,
        "lease_started_at": created_at,
        "status": "background",
        "conversation_thread_id": str(getattr(thread, "thread_id", "") or ""),
        "conversation_task_path": str(getattr(selected, "task_path", "") or ""),
        "conversation_task_link_status": str(getattr(selected, "status", "") or ""),
        "conversation_work_kind": str(getattr(selected, "work_kind", "") or ""),
        "conversation_work_name": str(getattr(selected, "work_name", "") or ""),
        "conversation_cancellation_scope": str(
            getattr(selected, "cancellation_scope", "") or "foreground"
        ),
    }
    return _GatewayRequestRecord(None, payload, "task")


def _conversation_link_has_live_executor(
    owner_agent: object,
    store: object,
    thread_id: str,
    link: object,
) -> bool:
    """Return true only for an executing turn, never for an open task or future reminder."""
    task_id = str(getattr(link, "task_id", "") or "").strip()
    if not task_id:
        return False
    if is_interruptible_registered(conversation_request_interrupt_name(task_id)):
        return True
    try:
        claim, error = store.load_background_run_claim_report(thread_id)
    except Exception:
        return False
    if error is not None or not isinstance(claim, dict):
        return False
    try:
        expires_at = float(claim.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return (
        str(claim.get("status") or "").strip().lower() == "running"
        and str(claim.get("task_id") or "").strip() == task_id
        and expires_at > time.time()
    )


def _control_active_conversation_links(
    store: object,
    thread_id: str,
    links: list[object],
) -> list[object]:
    """Resolve control-active roots from task projection plus durable execution state.

    A task link can cross to ``completed`` just before its background turn consumes
    newly queued guidance.  The exact live claim/progress policy remains the runtime
    authority during that handoff, matching 会话运行时's active-turn semantics.  An
    unreadable execution state never revives a terminal link.
    """
    from ..conversation.task_promotion import (
        conversation_thread_execution_state,
        is_reusable_conversation_workspace,
    )

    execution = conversation_thread_execution_state(store, thread_id)
    running_task_ids = execution.get("running_task_ids")
    running_task_ids = (
        {str(item) for item in running_task_ids}
        if isinstance(running_task_ids, list)
        else set()
    )
    execution_available = execution.get("state_available") is True
    candidates: list[object] = []
    for link in links:
        if not is_reusable_conversation_workspace(link):
            continue
        if str(getattr(link, "cancellation_scope", "") or "foreground") == "detached":
            continue
        status = str(getattr(link, "status", "") or "").strip().lower()
        if status == "active":
            candidates.append(link)
            continue
        if status != "completed":
            continue
        task_id = str(getattr(link, "task_id", "") or "")
        if execution_available and task_id in running_task_ids:
            candidates.append(link)
    return candidates


def _select_active_conversation_link(active: list[object], task_id: str):
    selected_id = str(task_id or "").strip()
    if selected_id:
        return next(
            (
                link
                for link in active
                if str(getattr(link, "task_id", "") or "").strip() == selected_id
            ),
            None,
        )
    return max(
        active,
        key=lambda link: (
            float(getattr(link, "created_at", 0.0) or 0.0),
            str(getattr(link, "task_id", "") or ""),
        ),
    )


def _linked_conversation_task_id(record: _GatewayRequestRecord | None) -> str:
    if record is None:
        return ""
    runtime = record.payload.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return ""
    return str(runtime.get("task_id") or "").strip()


# LLM: Corrupt request records cannot prove ownership and are therefore excluded fail-closed.
# 函数用途：读取目录中属于当前用户会话的请求记录。
def _matching_requests(folder: Path, scope: GatewayControlScope) -> list[_GatewayRequestRecord]:
    records: list[_GatewayRequestRecord] = []
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return records
    for path in paths:
        report = read_json_file_report(path, context="gateway.control.request.read")
        if report.load_error is not None or not report.payload:
            continue
        payload = dict(report.payload)
        if _request_matches_scope(payload, scope):
            records.append(_GatewayRequestRecord(path, payload))
    return records


# LLM: Even administrators get conversation-scoped selection unless they explicitly omit a user fact.
# 函数用途：比较请求里的结构化渠道、会话和用户身份。
def _request_matches_scope(payload: dict[str, object], scope: GatewayControlScope) -> bool:
    conversation = payload.get("conversation")
    conversation = conversation if isinstance(conversation, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    request_channel = str(conversation.get("channel") or metadata.get("channel") or "").strip()
    request_conversation = str(conversation.get("channel_conversation_id") or "").strip()
    request_user = str(
        payload.get("user_id")
        or conversation.get("canonical_user_id")
        or conversation.get("channel_user_id")
        or metadata.get("user_id")
        or ""
    ).strip()
    if scope.channel and request_channel != scope.channel:
        return False
    if scope.conversation_id and request_conversation != scope.conversation_id:
        return False
    if scope.user_id and request_user != scope.user_id:
        return False
    return bool(scope.conversation_id or (scope.all_user_access and scope.user_id))


# LLM: Steering targets exactly one active request or durable root task, never a later chat turn.
# 函数用途：把用户补充写入当前 owner 的当前任务引导收件箱。
def _steer_active_request(
    base_agent: object,
    active: _GatewayRequestRecord,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    request_id = _record_id(active)
    target_type = "task" if active.target_kind == "task" else "request"
    try:
        owner_agent = _request_agent(base_agent, active.payload)
        store = owner_agent.conversation_store
        thread_id = _control_thread_id(store, active, scope)
        if not thread_id:
            raise ValueError("conversation thread is unavailable")
        if target_type == "task":
            with store.task_transition_guard(request_id):
                if not _durable_task_is_current(owner_agent, active):
                    return ConversationControlResult(
                        "steer",
                        False,
                        "当前任务刚刚结束或已切换，补充要求未应用到下一任务。",
                        request_id=request_id,
                    )
                entry = _append_control_guidance(
                    store, target_type, request_id, command, scope, thread_id
                )
                # Binding a newer task does not take the old task's transition lock.
                # Re-check after the durable append, matching 会话运行时's expected-turn
                # guard: a steer that lost the current-turn race is retired and never
                # becomes input for either the old or the new task.
                if not _durable_task_is_current(owner_agent, active):
                    store.mark_guidance_delivered([entry.guidance_id])
                    return ConversationControlResult(
                        "steer",
                        False,
                        "当前任务刚刚结束或已切换，补充要求未应用到下一任务。",
                        request_id=request_id,
                    )
        else:
            entry = _append_control_guidance(
                store, target_type, request_id, command, scope, thread_id
            )
    except Exception:
        return ConversationControlResult(
            "steer",
            False,
            "当前任务的补充通道暂时不可用，补充要求未保存。",
            request_id=request_id,
        )
    if target_type == "task":
        if active.linked_request is None or _linked_request_target_state(
            active.linked_request, request_id
        ) == "retired":
            _wake_for_task_guidance(owner_agent, active, entry.guidance_id, scope)
    elif active.path is None or not active.path.exists():
        owner_agent.conversation_store.mark_guidance_delivered([entry.guidance_id])
        return ConversationControlResult(
            "steer",
            False,
            "当前任务刚刚结束，补充要求未应用到下一任务。",
            request_id=request_id,
        )
    return ConversationControlResult(
        "steer",
        True,
        "已补充到当前任务；代理会在下一个安全点按新要求调整。",
        request_id=request_id,
    )


def _append_control_guidance(
    store: object,
    target_type: str,
    request_id: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    thread_id: str,
):
    channel_message_id = str(scope.metadata.get("message_id") or "").strip()
    return store.append_guidance(
        {
            "target_type": target_type,
            "target_id": request_id,
            "message": command.value,
            "sender": scope.user_id,
            "priority": "high",
            "delivery": "current_task" if target_type == "task" else "current_request",
            "metadata": {
                "kind": "active_turn_user_input",
                "record_in_transcript": True,
                "thread_id": thread_id,
                "channel": scope.channel,
                "conversation_id": scope.conversation_id,
                "channel_message_id": channel_message_id,
            },
        }
    )


def _control_thread_id(
    store: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> str:
    direct = str(active.payload.get("conversation_thread_id") or "").strip()
    if direct:
        return direct
    runtime = active.payload.get("conversation_runtime")
    if isinstance(runtime, dict):
        direct = str(runtime.get("thread_id") or "").strip()
        if direct:
            return direct
    thread = store.resolve_thread(
        channel=scope.channel,
        channel_conversation_id=scope.conversation_id,
        channel_user_id=scope.user_id,
    )
    if thread is None:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": scope.user_id,
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "当前会话",
            }
        )
    return str(getattr(thread, "thread_id", "") or "").strip()


def _durable_task_is_current(owner_agent: object, active: _GatewayRequestRecord) -> bool:
    """会话运行时 expected-turn check: the selected task must still be current.

    The initial lookup and durable append cannot share one filesystem lock.  Re-resolve
    the latest user-selectable active root after the append; if another task became
    current, retire this steer instead of applying it to either task.
    """
    task_id = _record_id(active)
    if active.linked_request is not None:
        linked_state = _linked_request_target_state(active.linked_request, task_id)
        if linked_state == "current":
            return True
        if linked_state != "retired":
            return False
        # The foreground request file is atomically moved to done before the
        # durable continuation owns the next turn.  Keep the same TaskRun
        # steerable through that handoff, while the active-link check below
        # still rejects a real task switch.
    thread_id = str(active.payload.get("conversation_thread_id") or "").strip()
    try:
        links, load_errors = owner_agent.conversation_store.task_links_report(thread_id)
    except Exception:
        return False
    if load_errors:
        return False
    candidates = _control_active_conversation_links(
        owner_agent.conversation_store,
        thread_id,
        links,
    )
    if not candidates:
        return False
    current = max(
        candidates,
        key=lambda link: (
            float(getattr(link, "created_at", 0.0) or 0.0),
            str(getattr(link, "task_id", "") or ""),
        ),
    )
    return str(getattr(current, "task_id", "") or "").strip() == task_id


def _linked_request_target_state(linked: _GatewayRequestRecord, task_id: str) -> str:
    """Classify a claimed foreground turn without conflating handoff with corruption."""
    if linked.path is None:
        return "unavailable"
    if not linked.path.exists():
        return "retired"
    report = read_json_file_report(linked.path, context="gateway.control.linked_request.read")
    if report.load_error is not None or not report.payload:
        return "retired" if not linked.path.exists() else "unavailable"
    current = _GatewayRequestRecord(linked.path, dict(report.payload))
    matches = (
        _record_id(current) == _record_id(linked)
        and _linked_conversation_task_id(current) == task_id
        and not bool(current.payload.get("cancel_requested"))
    )
    return "current" if matches else "mismatch"


# LLM: Publish a wake only when no linked live execution turn exists; live turns consume the durable FIFO guidance in place.
# 函数用途：只唤醒当前没有执行线的持久任务，避免 `/btw` 为同一任务启动第二个主执行器。
def _wake_for_task_guidance(
    owner_agent: object,
    active: _GatewayRequestRecord,
    guidance_id: str,
    scope: GatewayControlScope,
) -> None:
    """Wake an idle durable root; a linked live turn consumes guidance in-place."""
    try:
        from ..conversation.task_promotion import conversation_task_execution_state

        thread_id = str(active.payload.get("conversation_thread_id") or "")
        execution = conversation_task_execution_state(
            owner_agent.conversation_store,
            thread_id,
            _record_id(active),
        )
        if execution.get("state_available") is not True:
            return
        if execution.get("running") is True:
            return
        reason = "user_guidance"
        metadata = {
            "guidance_id": guidance_id,
            "channel": scope.channel,
            "conversation_id": scope.conversation_id,
        }
        goal = owner_agent.conversation_store.load_goal(
            thread_id,
            task_id=_record_id(active),
        )
        if (
            goal is not None
            and goal.status == "active"
            and goal.task_id == _record_id(active)
        ):
            reason = "thread_goal_continue"
            metadata["goal_id"] = goal.goal_id
        owner_agent.conversation_store.raise_wake_signal(
            {
                "thread_id": thread_id,
                "root_task_id": _record_id(active),
                "urgency": "urgent",
                "reason": reason,
                "summary": "用户补充了当前任务要求。",
                "dedupe_key": f"guidance:{guidance_id}",
                "metadata": metadata,
            }
        )
    except Exception:
        return


# LLM: Stop persists run interruption before signalling workers; it must preserve the durable task/workspace for an explicit later resume.
# 函数用途：中断当前执行轮并异步回收其子代理进程，但不删除会话、任务或工作目录。
def _stop_active_request(
    base_agent: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    if active.target_kind == "task":
        return _stop_active_task(base_agent, active, scope)
    request_id = _record_id(active)
    marked, failure_message = _mark_request_stopping(active, scope)
    if not marked:
        return ConversationControlResult(
            "stop",
            False,
            failure_message,
            request_id=request_id,
        )
    interrupt_by_name(conversation_request_interrupt_name(request_id))
    _cancel_request_subagents_async(base_agent, active.payload, request_id)
    return ConversationControlResult(
        "stop",
        True,
        "已收到停止请求，当前任务正在停止。",
        request_id=request_id,
    )


# LLM: A window stop targets the exact live request before consulting durable task metadata.
# 函数用途：像停止按钮一样先打断本会话当前执行轮，再清理它绑定的任务和子代理。
def _stop_live_window_request(
    base_agent: object,
    live_request: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    request_id = _record_id(live_request)
    interrupted = interrupt_by_name(conversation_request_interrupt_name(request_id))
    marked, failure_message = _mark_request_stopping(live_request, scope)
    if not interrupted and not marked:
        return ConversationControlResult(
            "stop",
            False,
            failure_message or "当前执行刚刚结束，无需停止。",
            request_id=request_id,
        )

    linked_task_id = _linked_conversation_task_id(live_request)
    _retire_stopped_turn_guidance(
        base_agent,
        live_request,
        request_id=request_id,
        task_id="" if _request_is_detached(live_request) else linked_task_id,
    )
    if _request_is_detached(live_request):
        return ConversationControlResult(
            "stop",
            True,
            "已停止当前会话正在执行的内容。",
            request_id=request_id,
        )
    durable = (
        _linked_active_task_record(base_agent, live_request, scope, linked_task_id)
        if linked_task_id
        else None
    )
    if durable is not None:
        _stop_active_task(
            base_agent,
            durable,
            scope,
            linked_request_already_stopped=True,
        )
    else:
        _cancel_request_subagents_async(
            base_agent,
            live_request.payload,
            request_id,
        )
    return ConversationControlResult(
        "stop",
        True,
        "已停止当前会话正在执行的内容。",
        request_id=request_id,
    )


# LLM: Pending user steer belongs to the interrupted turn and must not leak into a later resume.
# 函数用途：停止当前会话轮次时确认尚未消费的 request/task 引导，但保留已写入的 transcript。
def _retire_stopped_turn_guidance(
    base_agent: object,
    live_request: _GatewayRequestRecord,
    *,
    request_id: str,
    task_id: str,
) -> None:
    try:
        store = _request_agent(base_agent, live_request.payload).conversation_store
        entries = list(store.pending_guidance("request", request_id, limit=0))
        if task_id:
            entries.extend(store.pending_guidance("task", task_id, limit=0))
        store.mark_guidance_delivered([entry.guidance_id for entry in entries])
    except Exception:
        return


def _mark_request_stopping(
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> tuple[bool, str]:
    """Persist cancellation on one exact live queue record."""
    request_id = _record_id(active)
    updated_ref = [False]

    def mark_cancel(current: dict) -> dict:
        path_stem = active.path.stem if active.path is not None else ""
        if str(current.get("id") or path_stem) != request_id:
            return current
        now = time.time()
        current.update(
            {
                "cancel_requested": True,
                "cancel_requested_at": now,
                "cancel_requested_by": scope.user_id,
                "control_status": "stopping",
                "updated_at": now,
            }
        )
        updated_ref[0] = True
        return current

    try:
        if active.path is None:
            raise FileNotFoundError(request_id)
        update_json_file_atomic(active.path, mark_cancel, require_existing=True)
    except FileNotFoundError:
        return False, "当前任务刚刚结束，无需停止。"
    except OSError:
        return False, "停止请求暂时无法保存，请稍后重试。"
    if not updated_ref[0]:
        return False, "当前任务刚刚结束，无需停止。"
    return True, ""


def _stop_active_task(
    base_agent: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
    *,
    linked_request_already_stopped: bool = False,
) -> ConversationControlResult:
    task_id = _record_id(active)
    linked_request_id = _record_id(active.linked_request)
    if linked_request_already_stopped:
        linked_marked = bool(linked_request_id)
    else:
        linked_request_id, linked_marked = _stop_linked_live_request(active, scope)
    task_interrupted = interrupt_by_name(conversation_request_interrupt_name(task_id))
    try:
        owner_agent, store, stopped = _interrupt_active_task_link(base_agent, active, task_id)
    except Exception:
        if linked_marked:
            interrupt_by_name(conversation_request_interrupt_name(linked_request_id))
        return ConversationControlResult(
            "stop",
            linked_marked or task_interrupted,
            (
                "已收到停止请求，当前任务正在停止。"
                if linked_marked or task_interrupted
                else "停止请求暂时无法保存，请稍后重试。"
            ),
            request_id=task_id,
        )
    if stopped is None and not linked_marked and not task_interrupted:
        return ConversationControlResult("stop", False, "当前任务刚刚结束，无需停止。")
    if stopped is not None:
        _pause_goal_for_stopped_task(store, stopped)
    run_ids = _related_control_run_ids(owner_agent, task_id, linked_request_id)
    for run_id in run_ids:
        try:
            store.update_task_status(
                {"task_id": run_id, "status": "cancelled", "expected_status": "active"}
            )
        except Exception:
            continue
    if stopped is not None:
        _interrupt_task_registry_record(owner_agent, task_id)
    if linked_request_id:
        interrupt_by_name(conversation_request_interrupt_name(linked_request_id))
    _cancel_request_subagents_async(
        base_agent,
        active.payload,
        task_id,
        run_ids=run_ids,
    )
    return ConversationControlResult(
        "stop",
        True,
        "已收到停止请求，当前任务正在停止。",
        request_id=task_id,
    )


def _interrupt_active_task_link(
    base_agent: object,
    active: _GatewayRequestRecord,
    task_id: str,
) -> tuple[object, object, object | None]:
    """CAS the exact projected status to interrupted under the task transition lock."""
    owner_agent = _request_agent(base_agent, active.payload)
    store = owner_agent.conversation_store
    expected_status = str(
        active.payload.get("conversation_task_link_status") or "active"
    ).strip().lower()
    with store.task_transition_guard(task_id):
        stopped = store.update_task_status(
            {
                "task_id": task_id,
                "status": "interrupted",
                "expected_status": expected_status,
            }
        )
    return owner_agent, store, stopped


def _stop_linked_live_request(
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> tuple[str, bool]:
    linked = active.linked_request
    linked_request_id = _record_id(linked)
    if linked is None:
        return linked_request_id, False
    marked, _failure = _mark_request_stopping(linked, scope)
    return linked_request_id, marked


def _related_control_run_ids(
    owner_agent: object,
    task_id: str,
    linked_request_id: str,
) -> list[str]:
    try:
        run_ids = owner_agent.subagent_run_ids_for_request(task_id)
        if linked_request_id:
            run_ids.extend(owner_agent.subagent_run_ids_for_request(linked_request_id))
        return list(dict.fromkeys(run_ids))
    except Exception:
        return []


# LLM: A 会话运行时 stop pauses an active goal bound to the interrupted task instead of clearing it.
# 函数用途: `/stop` 中断任务时同步暂停精确绑定的持续目标。
def _pause_goal_for_stopped_task(store: object, task_link: object) -> None:
    thread_id = str(getattr(task_link, "thread_id", "") or "")
    task_id = str(getattr(task_link, "task_id", "") or "")
    try:
        with store.goal_transition_guard(thread_id):
            goal = store.load_goal(thread_id, task_id=task_id)
            if goal is None or goal.status != "active" or goal.task_id != task_id:
                return
            store.update_goal(
                {
                    "thread_id": thread_id,
                    "goal_id": goal.goal_id,
                    "status": "paused",
                    "expected_status": "active",
                }
            )
    except Exception:
        return


# LLM: The global task projection records an interrupt as resumable, never as terminal cancellation.
# 函数用途：同步任务索引中的中断状态，后续明确续接时可以恢复为 running。
def _interrupt_task_registry_record(owner_agent: object, task_id: str) -> None:
    try:
        registry = owner_agent.local_store.task_registry
        current = registry.lookup_task(task_id)
        status = str((current or {}).get("status") or "").strip()
        if status:
            registry.update_task_status(task_id, "interrupted", expected_status=status)
    except Exception:
        return


# LLM: Child cancellation is best-effort and off the HTTP callback thread; the main stop ack stays immediate.
# 函数用途：后台取消当前请求派生的活跃子代理和进程。
def _cancel_request_subagents_async(
    base_agent: object,
    payload: dict[str, object],
    request_id: str,
    *,
    run_ids: list[str] | None = None,
) -> None:
    try:
        owner_agent = _request_agent(base_agent, payload)
        selected_run_ids = (
            list(run_ids)
            if run_ids is not None
            else owner_agent.subagent_run_ids_for_request(request_id)
        )
    except Exception:
        return

    def cancel() -> None:
        try:
            owner_agent.cancel_request_subagents(
                request_id,
                reason="conversation_user_stop",
                run_ids=selected_run_ids,
            )
        except Exception:
            return

    threading.Thread(target=cancel, name=f"cancel-{request_id}", daemon=True).start()


# LLM: Status projects only public runtime facts; tool names, commands, paths and steer history stay private.
# 函数用途：汇总当前请求、排队数、最近阶段、子代理和会话压缩状态。
def _gateway_task_status(
    base_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
    active: _GatewayRequestRecord | None,
) -> ConversationTaskStatus:
    queued = [
        record
        for record in _matching_requests(paths.inbox, scope)
        if not _request_is_detached(record)
    ]
    selected = active or (min(queued, key=lambda item: _request_timestamp(item.payload)) if queued else None)
    live_request = active.linked_request if active is not None else None
    display_selected = live_request or selected
    payload = display_selected.payload if display_selected is not None else _scope_request_payload(scope)
    owner_agent = _request_agent_or_base(base_agent, payload)
    compact_generation, verbose_level = _conversation_profile(owner_agent, payload)
    subagents = (
        _subagent_status(owner_agent, [_record_id(active), _record_id(live_request)])
        if active is not None
        else (0, 0, 0, 0)
    )
    state = "idle"
    if active is not None:
        if bool(payload.get("cancel_requested")):
            state = "stopping"
        elif _control_record_is_executing(owner_agent, active, subagents):
            state = "running"
        elif queued:
            state = "queued"
    elif queued:
        state = "queued"
    is_executing = state in {"running", "stopping"}
    started_at = _request_started_at(payload) if is_executing else 0.0
    progress_request_id = _record_id(live_request or active)
    return ConversationTaskStatus(
        state=state,
        task=_request_prompt(display_selected.payload) if display_selected is not None else "",
        elapsed_seconds=max(0.0, time.time() - started_at) if started_at else 0.0,
        queued_count=len(queued),
        recent_progress=_recent_progress(paths, progress_request_id) if is_executing else "",
        subagent_total=subagents[0],
        subagent_running=subagents[1],
        subagent_done=subagents[2],
        subagent_failed=subagents[3],
        model_name=str(getattr(getattr(owner_agent, "config", None), "model_name", "") or ""),
        compact_generation=compact_generation,
        verbose_level=verbose_level,
        durable_work=_named_durable_statuses(owner_agent, paths, scope),
    )


def _named_durable_statuses(
    owner_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> tuple[NamedConversationWorkStatus, ...]:
    try:
        store = owner_agent.conversation_store
        thread = store.resolve_thread(
            channel=scope.channel,
            channel_conversation_id=scope.conversation_id,
            channel_user_id=scope.user_id,
        )
        if thread is None:
            links, goals = [], []
        else:
            links, link_errors = store.task_links_report(thread.thread_id)
            goals = store.load_goals(thread.thread_id)
            if link_errors:
                return ()
    except Exception:
        return ()
    now_value = time.time()
    items: list[NamedConversationWorkStatus] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        if str(getattr(link, "work_kind", "") or "") != "audit":
            continue
        name = str(getattr(link, "work_name", "") or "").strip()
        if not name:
            continue
        status = str(getattr(link, "status", "") or "").strip().lower()
        if status in THREAD_TASK_LINK_INACTIVE_STATUSES:
            continue
        created_at = float(getattr(link, "created_at", 0.0) or 0.0)
        started_at = thread_task_run_started_at(link, fallback=created_at)
        audit_health = _audit_brief_health(owner_agent, link)
        items.append(
            NamedConversationWorkStatus(
                kind="audit",
                name=name,
                status=(
                    audit_health or "时长已到"
                    if status == "active"
                    and float(getattr(link, "expires_at", 0.0) or 0.0) > 0
                    and float(getattr(link, "expires_at", 0.0) or 0.0) <= now_value
                    else audit_health or _named_work_status_label(status)
                ),
                elapsed_seconds=max(
                    0.0,
                    now_value - (started_at or now_value),
                ),
            )
        )
        seen.add(("audit", name))
    for record in _pending_exact_audit_requests(paths, scope, ""):
        system_task = record.payload.get("system_task")
        attributes = system_task.get("attributes") if isinstance(system_task, dict) else {}
        name = (
            str(attributes.get("conversation_work_name") or "").strip()
            if isinstance(attributes, dict)
            else ""
        )
        if not name or ("audit", name) in seen:
            continue
        created_at = _request_timestamp(record.payload)
        items.append(
            NamedConversationWorkStatus(
                kind="audit",
                name=name,
                status=(
                    "排队中"
                    if record.path is not None and record.path.parent == paths.inbox
                    else "运行中"
                ),
                elapsed_seconds=max(0.0, now_value - created_at) if created_at else 0.0,
            )
        )
        seen.add(("audit", name))
    for goal in goals:
        name = str(getattr(goal, "name", "") or "").strip()
        status = str(getattr(goal, "status", "") or "").strip().lower()
        if not name or status == "complete":
            continue
        items.append(
            NamedConversationWorkStatus(
                kind="goal",
                name=name,
                status=_named_work_status_label(status),
                elapsed_seconds=float(store.current_goal_time_seconds(goal)),
            )
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (item.kind, item.name.casefold()),
        )
    )


def _audit_brief_health(owner_agent: object, link: object) -> str:
    """Project only actionable Audit health into the compact global status."""
    try:
        from ..ingestion.audit_state import audit_task_source_facts

        sources = audit_task_source_facts(
            owner_agent,
            str(getattr(link, "task_id", "") or ""),
        )
    except Exception:
        return ""
    if any(
        isinstance(source.get("source_worker"), dict)
        and source["source_worker"].get("state") == "awaiting_operator"
        for source in sources
    ):
        return "额度暂停"
    if any(
        isinstance(source.get("capacity"), dict)
        and isinstance(source["capacity"].get("capacity_alert"), dict)
        and source["capacity"]["capacity_alert"].get("active") is True
        for source in sources
    ):
        return "容量告警"
    return ""


def _named_work_status_label(status: str) -> str:
    return {
        "active": "运行中",
        "preparing": "准备中",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "usage_limited": "额度受限",
        "budget_limited": "时长或预算已到",
        "interrupted": "已停止",
    }.get(status, status or "未知")


# LLM: A durable task is resumable conversation state, not proof that an executor is live.
# 函数用途：只按 processing 记录、活跃子代理和持久执行台账判断当前任务是否真在运行。
def _control_record_is_executing(
    owner_agent: object,
    active: _GatewayRequestRecord,
    subagents: tuple[int, int, int, int],
) -> bool:
    if active.target_kind == "request" or active.linked_request is not None:
        return True
    if subagents[1] > 0:
        return True
    thread_id = str(active.payload.get("conversation_thread_id") or "").strip()
    task_id = _record_id(active)
    if not thread_id or not task_id:
        return True
    try:
        from ..conversation.task_promotion import conversation_task_execution_state

        execution = conversation_task_execution_state(
            owner_agent.conversation_store,
            thread_id,
            task_id,
        )
    except Exception:
        return True
    if execution.get("state_available") is not True:
        return True
    return execution.get("running") is True


# LLM: Owner resolution reuses the request worker's fail-closed multi-user boundary.
# 函数用途：按请求身份取得与真实执行相同的 owner-scoped agent。
def _request_agent(base_agent: object, payload: dict[str, object]):
    from .request_worker import _resolve_request_agent

    return _resolve_request_agent(base_agent, payload)


# LLM: Read-only status may degrade to base model facts when an idle owner cannot be materialized.
# 函数用途：状态查询尽量解析 owner；失败时只返回基础 agent，不扩大写权限。
def _request_agent_or_base(base_agent: object, payload: dict[str, object]):
    try:
        return _request_agent(base_agent, payload)
    except Exception:
        return base_agent


# LLM: Thread profile is resolved by the same structured channel binding as ordinary conversation history.
# 函数用途：读取当前会话已压缩次数和过程显示档位；未知时明确省略。
def _conversation_profile(agent: object, payload: dict[str, object]) -> tuple[int | None, str]:
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return None, ""
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, ""
    try:
        thread = store.resolve_thread(
            channel=str(conversation.get("channel") or ""),
            channel_conversation_id=str(conversation.get("channel_conversation_id") or ""),
            channel_user_id=str(conversation.get("channel_user_id") or ""),
        )
    except Exception:
        return None, ""
    if thread is None:
        return 0, "off"
    return int(getattr(thread, "compact_generation", 0) or 0), str(
        getattr(thread, "verbose_level", "off") or "off"
    )


# LLM: Subagent counts are derived from durable parent/root ids, never from streamed chatter.
# 函数用途：统计当前主请求派生子代理的运行、完成和异常数量。
def _subagent_status(agent: object, request_ids: list[str]) -> tuple[int, int, int, int]:
    selected_ids = list(dict.fromkeys(item for item in request_ids if item))
    if not selected_ids:
        return 0, 0, 0, 0
    try:
        request_run_ids = {
            run_id
            for request_id in selected_ids
            for run_id in agent.subagent_run_ids_for_request(request_id)
        }
        tasks = agent.subagents.list_runs()
    except Exception:
        return 0, 0, 0, 0
    related = [task for task in tasks if str(getattr(task, "id", "") or "") in request_run_ids]
    statuses = [str(getattr(task, "status", "") or "").upper() for task in related]
    running = sum(status in _ACTIVE_SUBAGENT_STATUSES for status in statuses)
    done = sum(status in _DONE_SUBAGENT_STATUSES for status in statuses)
    return len(related), running, done, max(0, len(related) - running - done)


# LLM: Progress summarizes typed phase/ok only; display text and raw tool data never drive /status.
# 函数用途：从最近一条 typed 工具事件生成不泄露内部执行细节的阶段描述。
def _recent_progress(paths: GatewayPaths, request_id: str) -> str:
    if not request_id:
        return ""
    for row in reversed(_tail_json_rows(gateway_chunk_path(paths, request_id))):
        if row.get("kind") != "tool_progress" or not isinstance(row.get("progress"), dict):
            continue
        phase = str(row["progress"].get("phase") or "")
        if phase == "started":
            return "正在执行一个步骤"
        if phase == "finished":
            if row["progress"].get("ok") is False:
                return "一个步骤失败，正在处理"
            return "刚完成一个执行步骤"
        if phase == "interrupted":
            return "正在停止"
    return "正在处理"


# LLM: The tail reader is byte-bounded so /status cannot load an unbounded stream file into memory.
# 函数用途：读取进度文件末尾至多 64 KiB 的 JSON 行。
def _tail_json_rows(path: Path, max_bytes: int = 64 * 1024) -> list[dict[str, object]]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# LLM: Synthetic idle status facts preserve the same owner and conversation schema as /ask.
# 函数用途：没有活跃请求时，根据控制请求身份构造只读会话定位信息。
def _scope_request_payload(scope: GatewayControlScope) -> dict[str, object]:
    # `/ask` and control commands must resolve the same owner for every
    # channel, including trusted local/CLI channels.  The authenticated scope
    # wins over optional message metadata so callers cannot redirect a control
    # command into another owner.
    metadata = {
        **dict(scope.metadata),
        "user_id": scope.user_id,
        "channel": scope.channel,
    }
    return {
        "user_id": scope.user_id,
        "metadata": metadata,
        "conversation": {
            "channel": scope.channel,
            "channel_conversation_id": scope.conversation_id,
            "channel_user_id": scope.user_id,
            "canonical_user_id": scope.user_id,
        },
    }


# LLM: Request ids are taken only from the claimed record or its filename.
# 函数用途：读取请求记录的稳定 id。
def _record_id(record: _GatewayRequestRecord | None) -> str:
    if record is None:
        return ""
    path_stem = record.path.stem if record.path is not None else ""
    return str(record.payload.get("id") or record.payload.get("request_id") or path_stem)


# LLM: Timestamp ordering uses durable queue/lease fields with a zero fallback.
# 函数用途：取得请求排序时间。
def _request_timestamp(payload: dict[str, object]) -> float:
    for key in ("lease_started_at", "created_at", "submitted_at"):
        try:
            if value := float(payload.get(key) or 0.0):
                return value
        except (TypeError, ValueError):
            continue
    return 0.0


# LLM: Elapsed time starts at the claimed lease, not user-controlled prompt metadata.
# 函数用途：取得任务实际开始时间。
def _request_started_at(payload: dict[str, object]) -> float:
    try:
        return float(payload.get("lease_started_at") or payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: Status exposes a bounded user prompt only; internal injection and tool plans are excluded.
# 函数用途：读取用户提交的任务正文。
def _request_prompt(payload: dict[str, object]) -> str:
    return str(payload.get("prompt") or payload.get("goal") or "").strip()


__all__ = [
    "GatewayControlScope",
    "execute_gateway_conversation_control",
    "steer_active_conversation_if_running",
]
