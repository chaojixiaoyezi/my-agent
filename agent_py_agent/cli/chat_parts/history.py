# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。


from __future__ import annotations

import threading
from dataclasses import dataclass

MAX_HISTORY_TURNS = 20
ASSISTANT_PREVIEW_CHARS = 500


# LLM: ConversationTurn 是chat CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass(frozen=True)
class ConversationTurn:
    user_message: str
    assistant_message: str


# LLM: build_history_context 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
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


# LLM: append_conversation_turn 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _preview_assistant_message centralizes chat-history truncation so config can tune it.
# 函数用途: 根据配置生成助手历史预览；0 或负数表示不截断，避免长任务丢关键上下文。
def _preview_assistant_message(message: str, max_chars: int) -> str:
    if max_chars <= 0 or len(message) <= max_chars:
        return message
    return message[:max_chars]


# LLM: chat config getters hide defensive casts from UI worker modules.
# 函数用途: 从 agent.config 读取聊天历史轮数，配置缺失或异常时回退到稳定默认值。
def chat_history_max_turns(config: object) -> int:
    try:
        return max(1, int(getattr(config, "chat_history_max_turns", MAX_HISTORY_TURNS) or MAX_HISTORY_TURNS))
    except (TypeError, ValueError):
        return MAX_HISTORY_TURNS


# LLM: chat config getters hide defensive casts from UI worker modules.
# 函数用途: 从 agent.config 读取助手历史预览字符数，0 表示不截断。
def chat_assistant_preview_chars(config: object) -> int:
    try:
        return max(0, int(getattr(config, "chat_history_assistant_preview_chars", ASSISTANT_PREVIEW_CHARS) or 0))
    except (TypeError, ValueError):
        return ASSISTANT_PREVIEW_CHARS
