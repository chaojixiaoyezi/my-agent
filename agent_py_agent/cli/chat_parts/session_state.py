
from __future__ import annotations

import threading
from typing import Optional

from ...agent.session import SessionManager, generate_session_id


def create_or_resume_session(
    session_manager: SessionManager,
    session_id: str | None,
    channel: str = "chat",
) -> tuple[SessionManager, str]:
    if session_id:
        session = session_manager.load_session(session_id)
        if session is None:
            print(f"会话 {session_id} 不存在，将创建新会话。", file=__import__("sys").stderr)
            session = session_manager.create_session(channel=channel)
        else:
            session_manager.touch_session(session.session_id, channel=channel)
            print(f"已恢复会话: {session.session_id}")
    else:
        session = session_manager.create_session(channel=channel)
        print(f"新会话: {session.session_id}")
    return session_manager, session.session_id


def touch_session_on_exit(session_manager: SessionManager, session_id: str, channel: str = "chat") -> None:
    session_manager.touch_session(session_id, channel=channel)


class ConversationHistory:

    def __init__(self, max_turns: int = 8) -> None:
        self._history: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._max_turns = max_turns

    def append(self, user: str, assistant: str) -> None:
        with self._lock:
            self._history.append((user, assistant))
            if len(self._history) > self._max_turns * 2:
                self._history[:] = self._history[-self._max_turns:]

    def get_context(self) -> str:
        with self._lock:
            if not self._history:
                return ""
            recent = self._history[-self._max_turns:]

        lines = ["## 最近对话上下文（供参考，按时间倒序）"]
        for user_msg, assistant_msg in reversed(recent):
            lines.append(f"用户: {user_msg}")
            lines.append(f"助手: {assistant_msg[:500]}")
        return "\n".join(lines)

    def __len__(self) -> int:
        with self._lock:
            return len(self._history)


__all__ = [
    "ConversationHistory",
    "create_or_resume_session",
    "touch_session_on_exit",
]