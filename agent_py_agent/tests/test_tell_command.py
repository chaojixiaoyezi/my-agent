"""`/tell` 与 `/sessions threads` TUI 命令的合同测试（无真实模型请求）。

覆盖：命令在 catalog 里、帮助文案可见、缺参数给用法、身份缺失 fail closed、
开关关闭返回 SESSION_MESSAGING_DISABLED、目标不存在返回越界码、成功入队并唤醒。
用 fake context，不启动 TUI/Gateway，不调用模型。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.command_catalog import COMMAND_CATALOG
from agent_py_agent.cli.chat_parts.slash_commands import (
    CHAT_HELP_TEXT,
    handle_common_slash_command,
)


def test_tell_and_sessions_threads_declared() -> None:
    names = {spec.name for spec in COMMAND_CATALOG}
    assert "tell" in names
    assert "/tell" in CHAT_HELP_TEXT
    assert "/sessions threads" in CHAT_HELP_TEXT


class _FakeThread:
    def __init__(self, thread_id: str, *, status: str = "active", title: str = "", channel: str = "") -> None:
        self.thread_id = thread_id
        self.status = status
        self.title = title
        bindings = (SimpleNamespace(channel=channel),) if channel else ()
        self.channel_bindings = bindings


class _FakeThreads:
    def __init__(self, known: dict) -> None:
        self._known = known

    def load_report(self, thread_id: str):
        return self._known.get(thread_id), None

    def list_report(self, *, limit: int = 100):
        return list(self._known.values()), []


class _FakeGuidance:
    def __init__(self) -> None:
        self.calls = []

    def append_once(self, request, *, dedupe_key):
        self.calls.append((request, dedupe_key))
        return SimpleNamespace(guidance_id="g-fixed")


class _FakeWakes:
    def __init__(self) -> None:
        self.signals = []

    def raise_signal(self, request):
        self.signals.append(request)
        return SimpleNamespace(wake_signal_id="w-fixed")


class _FakeStore:
    def __init__(self, known) -> None:
        self.threads = _FakeThreads(known)
        self.guidance = _FakeGuidance()
        self.wakes = _FakeWakes()


class _Ctx:
    def __init__(self, agent, *, conversation_id="thread-a") -> None:
        self.agent = agent
        self.conversation_id = conversation_id
        self.lines: list[str] = []

    def print_line(self, line: str) -> None:
        self.lines.append(line)


def _agent(known, *, owner=("local", "main", "main")):
    provider, kind, oid = owner
    from agent_py_agent.agent.capability.config import CapabilityConfig

    # 用真正的 CapabilityConfig：capability_config_for_agent 只认这个类型，
    # 替身对象会被跳过并回落到真实文件加载（那样测试改开关就无效）。
    config = CapabilityConfig()
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_provider=provider, owner_kind=kind, owner_id=oid),
        conversation_store=_FakeStore(known),
        _capability_config_runtime_snapshot=SimpleNamespace(config=config),
    )


def _run(user: str, ctx) -> bool:
    return handle_common_slash_command(user, ctx=ctx)


def test_tell_missing_args_shows_usage() -> None:
    ctx = _Ctx(_agent({}))
    assert _run("/tell", ctx) is True
    assert "用法" in "".join(ctx.lines)


def test_tell_unknown_target_out_of_scope() -> None:
    ctx = _Ctx(_agent({}))
    _run("/tell thread-missing 你好", ctx)
    text = "".join(ctx.lines)
    assert "SESSION_TARGET_OUT_OF_SCOPE" in text


def test_tell_identity_missing_fail_closed() -> None:
    ctx = _Ctx(_agent({}, owner=("", "", "")))
    _run("/tell thread-b 你好", ctx)
    assert "SESSION_IDENTITY_UNAVAILABLE" in "".join(ctx.lines)


def test_tell_switch_off_disabled() -> None:
    agent = _agent({"thread-b": _FakeThread("thread-b")})
    agent._capability_config_runtime_snapshot.config.session_messaging_admin_enabled = False
    ctx = _Ctx(agent)
    _run("/tell thread-b 你好", ctx)
    assert "SESSION_MESSAGING_DISABLED" in "".join(ctx.lines)


def test_tell_success_queues_and_wakes() -> None:
    agent = _agent({"thread-b": _FakeThread("thread-b")})
    ctx = _Ctx(agent)
    _run("/tell thread-b 你好", ctx)
    text = "".join(ctx.lines)
    assert "已耐久排队" in text
    assert "w-fixed" in text
    request, _key = agent.conversation_store.guidance.calls[0]
    assert request["metadata"]["origin_kind"] == "session_message"
    assert request["metadata"]["origin_thread_id"] == "thread-a"
    assert agent.conversation_store.wakes.signals[0]["reason"] == "session_message"


def test_tell_im_target_rejected() -> None:
    agent = _agent({"thread-im": _FakeThread("thread-im", channel="feishu")})
    ctx = _Ctx(agent)
    _run("/tell thread-im 你好", ctx)
    assert "SESSION_TARGET_CHANNEL_UNSUPPORTED" in "".join(ctx.lines)


def test_sessions_threads_lists_targets() -> None:
    agent = _agent({"thread-x": _FakeThread("thread-x", title="工作会话")})
    ctx = _Ctx(agent)
    assert _run("/sessions threads", ctx) is True
    text = "".join(ctx.lines)
    assert "thread-x" in text
    assert "工作会话" in text
    assert "/tell" in text
