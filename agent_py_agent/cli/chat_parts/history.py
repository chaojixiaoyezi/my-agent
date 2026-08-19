
# LLM: 本模块维护 CLI 进程内有界历史，并为显式 Gateway session 恢复提供 ConversationStore 的只读可见投影。
# 模块用途: 构造最近对话上下文、裁剪本地历史，并恢复已有聊天会话的完整问答。

from __future__ import annotations

import threading
from dataclasses import dataclass

MAX_HISTORY_TURNS = 20
ASSISTANT_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class ConversationTurn:
    user_message: str
    assistant_message: str


# LLM: GatewayChatHistorySnapshot 只投影 ConversationStore 中同一 chat session 的完整 user/assistant 对；错误必须保留给显式 resume fail-closed。
# 类用途: 保存从权威会话账本恢复出的可显示历史、线程身份和读取错误。
@dataclass(frozen=True)
class GatewayChatHistorySnapshot:
    turns: tuple[tuple[str, str], ...] = ()
    thread_id: str = ""
    load_errors: tuple[dict[str, object], ...] = ()


# LLM: 恢复只读 resolve_thread/recent_messages_report，不能创建线程、写第二 transcript 或按正文猜配对；request id 和 role 是唯一配对事实。
# 函数用途: 从当前 Agent 的 ConversationStore 恢复指定 CLI chat session 最近若干完整回合。
def load_gateway_chat_history(
    agent: object,
    session_id: str,
    *,
    max_turns: int,
) -> GatewayChatHistorySnapshot:
    if getattr(agent, "gateway_client_only", False) is True:
        return _load_thin_gateway_chat_history(agent, session_id, max_turns=max_turns)
    from ...agent.conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL

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
        rows, row_errors = store.recent_messages_report(
            thread_id,
            limit=max(16, max(1, int(max_turns or 1)) * 4),
        )
    except Exception as exc:  # noqa: BLE001 同上，不能静默恢复成空历史
        return GatewayChatHistorySnapshot(
            thread_id=thread_id,
            load_errors=(_history_load_error(exc),),
        )
    turns = _paired_gateway_chat_turns(rows, max_turns=max_turns)
    return GatewayChatHistorySnapshot(
        turns=turns,
        thread_id=thread_id,
        load_errors=tuple(dict(item) for item in row_errors if isinstance(item, dict)),
    )


# LLM: 轻量客户端只能消费 Gateway 的 typed history contract；端点不可用或行格式错误必须显式 fail-closed，不能构造本地 Agent。
# 函数用途: 从已运行 Gateway 恢复一个 TUI 会话的完整问答历史。
def _load_thin_gateway_chat_history(
    agent: object,
    session_id: str,
    *,
    max_turns: int,
) -> GatewayChatHistorySnapshot:
    payload = agent.request_chat_history(session_id, max_turns=max_turns)
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
        load_errors=errors,
    )


# LLM: 配对只接受 cli_chat 行、显式 gateway_request_id 和 user→assistant 角色；背景投递、残缺轮和未知角色不进入前台恢复历史。
# 函数用途: 将按时间排列的消息账本行整理成完整问答回合。
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
        elif role == "assistant" and request_id in user_by_request:
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
