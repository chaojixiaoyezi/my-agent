# Chat Refactor Plan

LLM: Split chat.py by interaction responsibility without changing CLI behavior.

给人看的解释：
`chat.py` 现在承担交互、渲染、命令处理和会话状态，后续应小步拆分。

## Target Modules

- `cli/chat/session.py`: chat session state and lifecycle.
- `cli/chat/rendering.py`: terminal output formatting.
- `cli/chat/slash_commands.py`: slash command parsing and dispatch.
- `cli/chat/history.py`: history loading and persistence.
- `cli/chat/input_loop.py`: prompt toolkit loop and interruption handling.

## Migration Order

1. Extract rendering helpers with snapshot-style tests.
2. Extract slash command parsing while keeping command output identical.
3. Extract history persistence behind a small interface.
4. Keep `cmd_chat` as the public command entrypoint.
5. Lower the guardrail line limit after each successful extraction.

## Compatibility Rules

- No prompt text changes unless covered by tests or explicitly requested.
- No changes to `.chat_history` path behavior in the same refactor.
- Interactive behavior must keep working with and without `prompt_toolkit`.

## Implemented

- `agent_py_agent/cli/chat_parts/history.py` owns bounded conversation-history context.
- `agent_py_agent/cli/chat_parts/rendering.py` owns colors, terminal rules, startup banner, progress bar, and response collapsing.
- `agent_py_agent/cli/chat_parts/slash_commands.py` owns common slash commands shared by TUI and fallback loops.
- `agent_py_agent/tests/test_chat_parts.py` covers extracted history and rendering behavior.
- `agent_py_agent/cli/chat.py` remains the public `cmd_chat` entrypoint for compatibility.
