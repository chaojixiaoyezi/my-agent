

from __future__ import annotations

import threading
from dataclasses import dataclass

MAX_HISTORY_TURNS = 20
ASSISTANT_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class ConversationTurn:
    user_message: str
    assistant_message: str


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
