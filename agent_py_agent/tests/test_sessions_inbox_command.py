"""`/sessions inbox` 接收方展示的合同测试（无真实模型请求）。

覆盖：命令在 catalog 与帮助里、本会话收到的消息/任务按来源与状态列出、回报也标成宿主来源、
别的会话的消息/任务不显示、没有内容时给明确空态、读坏账如实提示。用 fake context，不启动 TUI。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.command_catalog import COMMAND_CATALOG
from agent_py_agent.cli.chat_parts.slash_commands import (
    CHAT_HELP_TEXT,
    handle_common_slash_command,
)

_CURRENT = "thread-me"
_OTHER = "thread-other"


def test_sessions_inbox_declared() -> None:
    names = {spec.name for spec in COMMAND_CATALOG}
    assert "sessions" in names
    assert "/sessions inbox" in CHAT_HELP_TEXT


class _Entry:
    def __init__(self, *, kind: str, sender: str, message: str, delivered: float = 0.0) -> None:
        self.metadata = {"origin_kind": kind} if kind else {}
        self.sender = sender
        self.message = message
        self.delivered_at = delivered


class _FakeGuidance:
    def __init__(self, entries) -> None:
        self._entries = entries

    def recent_report(self, target_type: str, target_id: str, *, limit: int = 20):
        return list(self._entries.get(target_id, [])), []


class _FakeTasks:
    def __init__(self, rows) -> None:
        self._rows = rows

    def list_report(self, *, limit: int = 100):
        return list(self._rows), []


class _FakeThreads:
    def list_report(self, *, limit: int = 100):
        return [], []


class _FakeStore:
    def __init__(self, *, entries=None, tasks=()) -> None:
        self.threads = _FakeThreads()
        self.guidance = _FakeGuidance(entries or {})
        self.session_tasks = _FakeTasks(tasks)


class _Ctx:
    def __init__(self, agent, *, conversation_id=_CURRENT) -> None:
        self.agent = agent
        self.conversation_id = conversation_id
        self.lines: list[str] = []

    def print_line(self, line: str) -> None:
        self.lines.append(line)


def _ctx(*, entries=None, tasks=(), store=None) -> _Ctx:
    agent = SimpleNamespace(
        conversation_store=store if store is not None else _FakeStore(entries=entries, tasks=tasks),
    )
    return _Ctx(agent)


def _run(user: str, ctx) -> bool:
    return handle_common_slash_command(user, ctx=ctx)


def test_inbox_lists_received_message_with_source_and_state() -> None:
    ctx = _ctx(entries={_CURRENT: [_Entry(kind="session_message", sender=_OTHER, message="帮我看下 X")]})

    assert _run("/sessions inbox", ctx) is True

    text = "".join(ctx.lines)
    assert "本会话收到的会话间消息" in text
    assert _OTHER in text and "帮我看下 X" in text and "待注入" in text


def test_inbox_lists_received_task_with_status_and_hides_other_threads() -> None:
    mine = SimpleNamespace(
        task_id="stask-1", target_thread_id=_CURRENT, sender_thread_id=_OTHER, status="accepted", goal="做 Y"
    )
    theirs = SimpleNamespace(
        task_id="stask-2", target_thread_id="thread-someone-else", sender_thread_id=_OTHER, status="queued", goal="做 Z"
    )
    ctx = _ctx(tasks=(mine, theirs))

    _run("/sessions inbox", ctx)

    text = "".join(ctx.lines)
    assert "本会话收到的派活任务" in text
    assert "stask-1" in text and _OTHER in text and "accepted" in text
    assert "stask-2" not in text


def test_inbox_labels_task_result_as_host_source() -> None:
    ctx = _ctx(entries={_CURRENT: [_Entry(kind="session_task_result", sender=_OTHER, message="任务已结束", delivered=1.0)]})

    _run("/sessions inbox", ctx)

    text = "".join(ctx.lines)
    assert "任务回报" in text and "已注入" in text


def test_inbox_ignores_plain_user_input() -> None:
    ctx = _ctx(entries={_CURRENT: [_Entry(kind="", sender="user", message="我自己说的话")]})

    _run("/sessions inbox", ctx)

    assert "我自己说的话" not in "".join(ctx.lines)
    assert "还没有收到" in "".join(ctx.lines)


def test_inbox_empty_state_is_explicit() -> None:
    ctx = _ctx()

    _run("/sessions inbox", ctx)

    assert "还没有收到会话间消息或派活任务" in "".join(ctx.lines)


def test_inbox_without_conversation_id_is_refused() -> None:
    ctx = _Ctx(SimpleNamespace(conversation_store=_FakeStore()), conversation_id="")

    _run("/sessions inbox", ctx)

    assert "没有会话编号" in "".join(ctx.lines)


def test_inbox_without_store_is_refused() -> None:
    ctx = _Ctx(SimpleNamespace(conversation_store=None))

    _run("/sessions inbox", ctx)

    assert "没有会话存储" in "".join(ctx.lines)
