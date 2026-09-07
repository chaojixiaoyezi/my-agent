
# LLM: 本模块维护 CLI 进程内有界历史，并为显式 Gateway session 恢复提供 ConversationStore 的只读可见投影。
# 模块用途: 构造最近对话预览；恢复时另传 canonical 显示事件，避免把问答预览误当完整正文。

from __future__ import annotations

import threading
from dataclasses import dataclass

MAX_HISTORY_TURNS = 20
ASSISTANT_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class ConversationTurn:
    user_message: str
    assistant_message: str


# LLM: Snapshot 分开保存预览、display-only 事件和双向游标；上翻只更新显示，不把旧页追加到模型预览。
# 类用途: 保存同一会话的预览、完整显示、线程身份与读取错误。
@dataclass(frozen=True)
class GatewayChatHistorySnapshot:
    turns: tuple[tuple[str, str], ...] = ()
    thread_id: str = ""
    load_errors: tuple[dict[str, object], ...] = ()
    display_events: tuple[dict[str, object], ...] | None = None
    message_cursor: int = 0
    before_message_cursor: int = 0


# LLM: 恢复和上翻共用只读 canonical 分页；不创建线程、不写第二 transcript，不从正文猜回合。
# 函数用途: 恢复最新窗口或更早的一页，同时传递各自的显示和实时读取边界。
def load_gateway_chat_history(
    agent: object,
    session_id: str,
    *,
    max_turns: int,
    before_message_cursor: int | None = None,
) -> GatewayChatHistorySnapshot:
    if getattr(agent, "gateway_client_only", False) is True:
        return _load_thin_gateway_chat_history(
            agent, session_id, max_turns=max_turns, before_message_cursor=before_message_cursor,
        )
    from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from ...agent.conversation.history_display import conversation_history_display_events

    selected_session_id = str(session_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not selected_session_id or store is None:
        return GatewayChatHistorySnapshot()
    try:
        thread, thread_error = store.resolve_thread_report(
            channel=LOCAL_CHAT_CHANNEL,
            channel_conversation_id=selected_session_id,
            channel_user_id=LOCAL_AGENT_USER_ID,
        )
    except Exception as exc:  # noqa: BLE001 恢复入口必须把存储异常转成显式只读错误
        return GatewayChatHistorySnapshot(load_errors=(_history_load_error(exc),))
    if thread_error is not None:
        return GatewayChatHistorySnapshot(load_errors=(dict(thread_error),))
    if thread is None:
        return GatewayChatHistorySnapshot()
    thread_id = str(getattr(thread, "thread_id", "") or "")
    try:
        page = store.history_page_report(
            thread_id,
            before=before_message_cursor,
            limit=max(16, max(1, int(max_turns or 1)) * 4),
        )
    except Exception as exc:  # noqa: BLE001 同上，不能静默恢复成空历史
        return GatewayChatHistorySnapshot(
            thread_id=thread_id,
            load_errors=(_history_load_error(exc),),
        )
    turns = _paired_gateway_chat_turns(list(page.rows), max_turns=max_turns)
    return GatewayChatHistorySnapshot(
        turns=turns,
        thread_id=thread_id,
        load_errors=page.errors,
        display_events=conversation_history_display_events(page.rows),
        message_cursor=page.after,
        before_message_cursor=page.before,
    )


# LLM: 轻量客户端只解析公开历史和整数游标；失败不回落本地 Agent，也不重置已有展示窗口。
# 函数用途: 从 Gateway 取得一页可见事件，错误留给启动或翻页调用方解释。
def _load_thin_gateway_chat_history(
    agent: object,
    session_id: str,
    *,
    max_turns: int,
    before_message_cursor: int | None = None,
) -> GatewayChatHistorySnapshot:
    options = {"before_message_cursor": before_message_cursor} if before_message_cursor is not None else {}
    payload = agent.request_chat_history(session_id, max_turns=max_turns, **options)
    payload = payload if isinstance(payload, dict) else {}
    raw_turns = payload.get("turns")
    turns: list[tuple[str, str]] = []
    if isinstance(raw_turns, list):
        for item in raw_turns:
            if not isinstance(item, dict):
                continue
            user_message = str(item.get("user_message") or "")
            assistant_message = str(item.get("assistant_message") or "")
            if user_message and assistant_message:
                turns.append((user_message, assistant_message))
    raw_errors = payload.get("load_errors")
    errors = tuple(
        dict(item)
        for item in raw_errors
        if isinstance(item, dict)
    ) if isinstance(raw_errors, list) else ()
    if not payload.get("ok") and not errors:
        errors = ({"error_code": "GATEWAY_HISTORY_UNAVAILABLE"},)
    return GatewayChatHistorySnapshot(
        turns=tuple(turns[-max(1, int(max_turns or 1)):]),
        thread_id=str(payload.get("thread_id") or ""),
        message_cursor=max(0, int(payload.get("message_cursor") or 0)),
        before_message_cursor=max(0, int(payload.get("before_message_cursor") or 0)),
        load_errors=errors,
        display_events=(
            tuple(dict(item) for item in payload["display_events"] if isinstance(item, dict))
            if isinstance(payload.get("display_events"), list) else None
        ),
    )


# LLM: Paired preview selects only assistant_part_id=final (legacy missing means final).
# Commentary remains in the full transcript/context but cannot replace the terminal turn preview.
# 函数用途: 将按时间排列的消息账本整理成最终问答预览，不让过程回复顶掉最终回复。
def _paired_gateway_chat_turns(
    rows: list[object],
    *,
    max_turns: int,
) -> tuple[tuple[str, str], ...]:
    from ...agent.conversation.channels import LOCAL_CHAT_SOURCE
    from ...agent.conversation.models import is_audit_background_transcript_entry

    user_by_request: dict[str, str] = {}
    assistant_by_request: dict[str, str] = {}
    request_order: list[str] = []
    for row in rows:
        if (
            str(getattr(row, "channel", "") or "") != LOCAL_CHAT_SOURCE
            or is_audit_background_transcript_entry(row)
        ):
            continue
        metadata = getattr(row, "metadata", {})
        request_id = str(
            metadata.get("gateway_request_id") if isinstance(metadata, dict) else ""
        ).strip()
        role = str(getattr(row, "role", "") or "").strip().lower()
        content = str(getattr(row, "content", "") or "")
        if not request_id or not content:
            continue
        if role == "user":
            if request_id not in user_by_request:
                request_order.append(request_id)
            user_by_request[request_id] = content
        elif (
            role == "assistant"
            and request_id in user_by_request
            and str(metadata.get("assistant_part_id") or "final") == "final"
        ):
            assistant_by_request[request_id] = content
    complete = tuple(
        (user_by_request[request_id], assistant_by_request[request_id])
        for request_id in request_order
        if request_id in assistant_by_request
    )
    limit = max(1, int(max_turns or 1))
    return complete[-limit:]


# LLM: CLI 只需要稳定错误类别，不把可能含 owner 路径的异常正文显示给用户。
# 函数用途: 把会话恢复异常转成最小结构化错误。
def _history_load_error(exc: BaseException) -> dict[str, object]:
    return {
        "error_code": "chat_history_load_failed",
        "error_type": type(exc).__name__,
    }


def build_history_context(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
    assistant_preview_chars: int = ASSISTANT_PREVIEW_CHARS,
) -> str:
    with history_lock:
        if not conversation_history:
            return ""
        recent = conversation_history[-max_turns:]
    lines = ["## 最近对话上下文（供参考，按时间倒序）"]
    for user_msg, agent_msg in reversed(recent):
        lines.append(f"用户: {user_msg}")
        lines.append(f"助手: {_preview_assistant_message(agent_msg, assistant_preview_chars)}")
    return "\n".join(lines)


def append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    turn: ConversationTurn,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> None:
    with history_lock:
        conversation_history.append((turn.user_message, turn.assistant_message))
        if len(conversation_history) > max_turns:
            conversation_history[:] = conversation_history[-max_turns:]


def _preview_assistant_message(message: str, max_chars: int) -> str:
    if max_chars <= 0 or len(message) <= max_chars:
        return message
    return message[:max_chars]


def chat_history_max_turns(config: object) -> int:
    try:
        return max(1, int(getattr(config, "chat_history_max_turns", MAX_HISTORY_TURNS) or MAX_HISTORY_TURNS))
    except (TypeError, ValueError):
        return MAX_HISTORY_TURNS


def chat_assistant_preview_chars(config: object) -> int:
    try:
        return max(0, int(getattr(config, "chat_history_assistant_preview_chars", ASSISTANT_PREVIEW_CHARS) or 0))
    except (TypeError, ValueError):
        return ASSISTANT_PREVIEW_CHARS
