# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。


from __future__ import annotations

import threading
from typing import Optional

from ...agent.session import SessionManager, generate_session_id


# LLM: create_or_resume_session 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: touch_session_on_exit 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def touch_session_on_exit(session_manager: SessionManager, session_id: str, channel: str = "chat") -> None:
    session_manager.touch_session(session_id, channel=channel)


# LLM: ConversationHistory 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ConversationHistory:

    # LLM: __init__ 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, max_turns: int = 8, assistant_preview_chars: int = 500) -> None:
        self._history: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._max_turns = max_turns
        self._assistant_preview_chars = assistant_preview_chars

    # LLM: append 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def append(self, user: str, assistant: str) -> None:
        with self._lock:
            self._history.append((user, assistant))
            if len(self._history) > self._max_turns:
                self._history[:] = self._history[-self._max_turns:]

    # LLM: get_context 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def get_context(self) -> str:
        with self._lock:
            if not self._history:
                return ""
            recent = self._history[-self._max_turns:]

        lines = ["## 最近对话上下文（供参考，按时间倒序）"]
        for user_msg, assistant_msg in reversed(recent):
            lines.append(f"用户: {user_msg}")
            lines.append(f"助手: {self._assistant_preview(assistant_msg)}")
        return "\n".join(lines)

    # LLM: _assistant_preview mirrors chat history rendering so session state honors config.
    # 函数用途: 根据构造参数截断助手历史；0 或负数表示不截断。
    def _assistant_preview(self, text: str) -> str:
        if self._assistant_preview_chars <= 0 or len(text) <= self._assistant_preview_chars:
            return text
        return text[: self._assistant_preview_chars]

    # LLM: __len__ 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __len__(self) -> int:
        with self._lock:
            return len(self._history)


__all__ = [
    "ConversationHistory",
    "create_or_resume_session",
    "touch_session_on_exit",
]
