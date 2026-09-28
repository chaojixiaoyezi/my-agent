"""会话间发消息工具的合同测试（无真实模型请求）。

覆盖：工具注册进门控前、缺少参数的结构化失败、目标会话不存在时返回统一越界码
（不泄露存在性）、以及成功路径把来源结构化的消息幂等写入目标 guidance 队列。
用 fake 的最小 agent/store，不启动 Gateway，不调用模型。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration.tools.send_session_message import (
    SendSessionMessageTool,
)
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_TARGET_OUT_OF_SCOPE,
)


class _FakeThread:
    def __init__(self, thread_id: str, *, status: str = "active", owner_id: str = "main") -> None:
        self.thread_id = thread_id
        self.status = status
        self.owner_id = owner_id


class _FakeThreads:
    def __init__(self, known: dict[str, _FakeThread]) -> None:
        self._known = known

    def load_report(self, thread_id: str):
        thread = self._known.get(thread_id)
        return thread, None


class _FakeGuidance:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []

    def append_once(self, request: dict, *, dedupe_key: str):
        self.calls.append((request, dedupe_key))

        class _Entry:
            guidance_id = "guidance-fixed"

        return _Entry()


class _FakeWakes:
    def __init__(self) -> None:
        self.signals: list[dict] = []

    def raise_signal(self, request: dict):
        self.signals.append(request)

        class _Signal:
            wake_signal_id = "wake-fixed"

        return _Signal()


class _FakeStore:
    def __init__(self, known: dict[str, _FakeThread]) -> None:
        self.threads = _FakeThreads(known)
        self.guidance = _FakeGuidance()
        self.wakes = _FakeWakes()


class _FakeHome:
    owner_provider = "local"
    owner_kind = "main"
    owner_id = "main"


class _FakeConfig:
    session_messaging_admin_enabled = True
    session_messaging_user_enabled = False
    session_task_admin_enabled = True


class _FakeAgent:
    def __init__(self, store: _FakeStore, *, current_thread: str = "thread-a") -> None:
        self.conversation_store = store
        self.home_paths = _FakeHome()
        self.capability_config = _FakeConfig()
        self._current_run_params = type(
            "_P", (), {"task_attributes": {"conversation_thread_id": current_thread}}
        )()


def _params(**overrides):
    base = {"target_thread_id": "thread-b", "message": "你好"}
    base.update(overrides)
    return base


def test_missing_target_is_structured_failure() -> None:
    agent = _FakeAgent(_FakeStore({"thread-b": _FakeThread("thread-b")}))
    outcome = SendSessionMessageTool(agent).execute(_params(target_thread_id=""))
    assert not outcome.ok
    assert outcome.error_code == "TOOL_PARAMETER_REQUIRED"


def test_missing_message_is_structured_failure() -> None:
    agent = _FakeAgent(_FakeStore({"thread-b": _FakeThread("thread-b")}))
    outcome = SendSessionMessageTool(agent).execute(_params(message="  "))
    assert not outcome.ok
    assert outcome.error_code == "TOOL_PARAMETER_REQUIRED"


def test_unknown_target_returns_out_of_scope() -> None:
    agent = _FakeAgent(_FakeStore({}))
    outcome = SendSessionMessageTool(agent).execute(_params(target_thread_id="thread-missing"))
    assert not outcome.ok
    assert outcome.error_code == SESSION_TARGET_OUT_OF_SCOPE
    # 目标不存在时不应产生任何入队或唤醒副作用。
    assert agent.conversation_store.guidance.calls == []
    assert agent.conversation_store.wakes.signals == []


def test_success_queues_idempotent_message_and_wakes_idle_target() -> None:
    store = _FakeStore({"thread-b": _FakeThread("thread-b", status="active")})
    agent = _FakeAgent(store)
    outcome = SendSessionMessageTool(agent).execute(_params())
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["delivery"] == "queued"
    assert payload["message_id"] == "guidance-fixed"
    assert payload["wake_signal_id"] == "wake-fixed"

    request, dedupe_key = store.guidance.calls[0]
    assert request["target_type"] == "thread"
    assert request["target_id"] == "thread-b"
    # 来源必须结构化：注入时据此渲染“来自会话 X 的消息”，不冒充用户原话。
    assert request["metadata"]["origin_kind"] == "session_message"
    assert request["metadata"]["origin_thread_id"] == "thread-a"
    assert dedupe_key
    assert store.wakes.signals[0]["reason"] == "session_message"


def test_inactive_target_is_queued_but_not_woken() -> None:
    store = _FakeStore({"thread-b": _FakeThread("thread-b", status="completed")})
    agent = _FakeAgent(store)
    outcome = SendSessionMessageTool(agent).execute(_params())
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["wake_signal_id"] == ""
    assert len(store.guidance.calls) == 1
    assert store.wakes.signals == []
