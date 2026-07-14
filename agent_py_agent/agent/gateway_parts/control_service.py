from __future__ import annotations

"""Conversation task controls backed by the authoritative Gateway request ledger.

给人看的解释：
飞书或终端发来的控制命令在这里查找“这个用户、这个会话”正在运行的请求。
它只允许纠偏和停止自己的当前任务，并从真实请求、会话与子代理记录生成状态。
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..concurrency.interrupt import interrupt_by_name
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    ConversationTaskStatus,
    conversation_request_interrupt_name,
    render_conversation_task_status,
)
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
    path: Path
    payload: dict[str, object]


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
    active = _active_request(paths, scope)
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
        action = "补充要求未保存" if command.kind == "steer" else "无需停止"
        return ConversationControlResult(
            command.kind,
            False,
            f"当前没有运行中的任务，{action}。",
        )
    if command.kind == "steer":
        return _steer_active_request(agent, active, command, scope)
    return _stop_active_request(agent, active, scope)


# LLM: Request selection uses structured owner/channel/conversation facts and never message text.
# 函数用途：找到当前会话唯一的 processing 请求；同会话单飞时通常只有一个。
def _active_request(paths: GatewayPaths, scope: GatewayControlScope) -> _GatewayRequestRecord | None:
    records = _matching_requests(paths.processing, scope)
    if not records:
        return None
    return max(records, key=lambda item: _request_timestamp(item.payload))


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


# LLM: Steering is persisted against only the active request id, so it cannot bleed into later turns.
# 函数用途：把用户补充写入当前 owner 的一次性引导收件箱。
def _steer_active_request(
    base_agent: object,
    active: _GatewayRequestRecord,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    request_id = _record_id(active)
    try:
        owner_agent = _request_agent(base_agent, active.payload)
        entry = owner_agent.conversation_store.append_guidance(
            {
                "target_type": "request",
                "target_id": request_id,
                "message": command.value,
                "sender": scope.user_id,
                "priority": "high",
                "delivery": "current_request",
                "metadata": {"channel": scope.channel, "conversation_id": scope.conversation_id},
            }
        )
    except Exception:
        return ConversationControlResult(
            "steer",
            False,
            "当前任务的补充通道暂时不可用，补充要求未保存。",
            request_id=request_id,
        )
    if not active.path.exists():
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


# LLM: Stop persists intent before signalling the in-process worker, making restart recovery fail-safe.
# 函数用途：给当前请求落停止标记、递协作中断，并异步回收它的子代理树。
def _stop_active_request(
    base_agent: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    request_id = _record_id(active)
    updated_ref = [False]

    def mark_cancel(current: dict) -> dict:
        if str(current.get("id") or active.path.stem) != request_id:
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
        update_json_file_atomic(active.path, mark_cancel, require_existing=True)
    except FileNotFoundError:
        return ConversationControlResult("stop", False, "当前任务刚刚结束，无需停止。")
    except OSError:
        return ConversationControlResult(
            "stop",
            False,
            "停止请求暂时无法保存，请稍后重试。",
            request_id=request_id,
        )
    if not updated_ref[0]:
        return ConversationControlResult("stop", False, "当前任务刚刚结束，无需停止。")
    interrupt_by_name(conversation_request_interrupt_name(request_id))
    _cancel_request_subagents_async(base_agent, active.payload, request_id)
    return ConversationControlResult(
        "stop",
        True,
        "已收到停止请求，当前任务正在停止。",
        request_id=request_id,
    )


# LLM: Child cancellation is best-effort and off the HTTP callback thread; the main stop ack stays immediate.
# 函数用途：后台取消当前请求派生的活跃子代理和进程。
def _cancel_request_subagents_async(base_agent: object, payload: dict[str, object], request_id: str) -> None:
    try:
        owner_agent = _request_agent(base_agent, payload)
        run_ids = owner_agent.subagent_run_ids_for_request(request_id)
    except Exception:
        return

    def cancel() -> None:
        try:
            owner_agent.cancel_request_subagents(
                request_id,
                reason="conversation_user_stop",
                run_ids=run_ids,
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
    queued = _matching_requests(paths.inbox, scope)
    selected = active or (min(queued, key=lambda item: _request_timestamp(item.payload)) if queued else None)
    payload = selected.payload if selected is not None else _scope_request_payload(scope)
    owner_agent = _request_agent_or_base(base_agent, payload)
    compact_generation, verbose_level = _conversation_profile(owner_agent, payload)
    subagents = _subagent_status(owner_agent, _record_id(active)) if active is not None else (0, 0, 0, 0)
    state = "idle"
    if active is not None:
        state = "stopping" if bool(active.payload.get("cancel_requested")) else "running"
    elif queued:
        state = "queued"
    started_at = _request_started_at(active.payload) if active is not None else 0.0
    return ConversationTaskStatus(
        state=state,
        task=_request_prompt(selected.payload) if selected is not None else "",
        elapsed_seconds=max(0.0, time.time() - started_at) if started_at else 0.0,
        queued_count=len(queued),
        recent_progress=_recent_progress(paths, _record_id(active)) if active is not None else "",
        subagent_total=subagents[0],
        subagent_running=subagents[1],
        subagent_done=subagents[2],
        subagent_failed=subagents[3],
        model_name=str(getattr(getattr(owner_agent, "config", None), "model_name", "") or ""),
        compact_generation=compact_generation,
        verbose_level=verbose_level,
    )


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
def _subagent_status(agent: object, request_id: str) -> tuple[int, int, int, int]:
    if not request_id:
        return 0, 0, 0, 0
    try:
        request_run_ids = set(agent.subagent_run_ids_for_request(request_id))
        tasks = agent.subagents.list_runs()
    except Exception:
        return 0, 0, 0, 0
    related = [task for task in tasks if str(getattr(task, "id", "") or "") in request_run_ids]
    statuses = [str(getattr(task, "status", "") or "").upper() for task in related]
    running = sum(status in _ACTIVE_SUBAGENT_STATUSES for status in statuses)
    done = sum(status in _DONE_SUBAGENT_STATUSES for status in statuses)
    return len(related), running, done, max(0, len(related) - running - done)


# LLM: Progress summarizes typed event status only; raw tool identity/detail/output never enters /status.
# 函数用途：从最近一条 typed 工具事件生成不泄露内部执行细节的阶段描述。
def _recent_progress(paths: GatewayPaths, request_id: str) -> str:
    if not request_id:
        return ""
    for row in reversed(_tail_json_rows(gateway_chunk_path(paths, request_id))):
        if row.get("kind") != "tool_progress" or not isinstance(row.get("progress"), dict):
            continue
        status = str(row["progress"].get("status") or "")
        if status == "开始":
            return "正在执行一个步骤"
        if status == "完成":
            return "刚完成一个执行步骤"
        if status == "中断":
            return "正在停止"
        if status.startswith("失败"):
            return "一个步骤失败，正在处理"
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
    metadata = dict(scope.metadata)
    if scope.channel not in {"local", "cli", "chat", "gateway-cli", "http"}:
        metadata = {"user_id": scope.user_id, "channel": scope.channel, **metadata}
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
    return str(record.payload.get("id") or record.payload.get("request_id") or record.path.stem)


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
]
