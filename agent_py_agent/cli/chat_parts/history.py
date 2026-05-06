
from __future__ import annotations

import threading

MAX_HISTORY_TURNS = 8


def build_history_context(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> str:
    with history_lock:
        if not conversation_history:
            return ""
        recent = conversation_history[-max_turns:]
    lines = ["## 最近对话上下文（供参考，按时间倒序）"]
    for user_msg, agent_msg in reversed(recent):
        lines.append(f"用户: {user_msg}")
        lines.append(f"助手: {agent_msg[:500]}")
    return "\n".join(lines)


def append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock: threading.Lock,
    user_message: str,
    assistant_message: str,
    *,
    max_turns: int = MAX_HISTORY_TURNS,
) -> None:
    with history_lock:
        conversation_history.append((user_message, assistant_message))
        if len(conversation_history) > max_turns * 2:
            conversation_history[:] = conversation_history[-max_turns:]