"""create_session_task 工具的合同测试（无真实模型请求）。

覆盖：缺参数失败、目标不存在越界、权限拒绝、IM 目标拒绝、成功建记录 + 投正文 + 唤醒、
正文只存一份（body_guidance_id 指向 guidance）、链深超限拒绝。
用 fake 的最小 agent/store，不启动 Gateway，不调用模型。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    CreateSessionTaskTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_TARGET_CHANNEL_UNSUPPORTED,
    SESSION_TARGET_OUT_OF_SCOPE,
    SESSION_TASK_CHAIN_LIMIT,
)


class _FakeThread:
    def __init__(self, thread_id, *, status="active", channel=""):
        self.thread_id = thread_id
        self.status = status
        from types import SimpleNamespace

        self.channel_bindings = (SimpleNamespace(channel=channel),) if channel else ()
        self.owner_id = "main"


class _FakeThreads:
    def __init__(self, known):
        self._known = known

    def load_report(self, thread_id):
        return self._known.get(thread_id), None


class _FakeGuidance:
    def __init__(self):
        self.calls = []

    def append_once(self, request, *, dedupe_key):
        self.calls.append((request, dedupe_key))
        from types import SimpleNamespace

        return SimpleNamespace(guidance_id=f"g-{len(self.calls)}")


class _FakeWakes:
    def __init__(self):
        self.signals = []

    def raise_signal(self, request):
        self.signals.append(request)
        from types import SimpleNamespace

        return SimpleNamespace(wake_signal_id="w-fixed")


class _FakeStore:
    def __init__(self, conversations_root, known):
        from agent_py_agent.agent.conversation import ConversationStore

        inner = ConversationStore(conversations_root)
        self.threads = _FakeThreads(known)
        self.guidance = _FakeGuidance()
        self.wakes = _FakeWakes()
        self.session_tasks = inner.session_tasks


class _Home:
    def __init__(self, owner):
        self.owner_provider, self.owner_kind, self.owner_id = owner


class _Agent:
    def __init__(self, store, *, owner=("local", "main", "main"), current_thread="thread-a", config=None):
        self.conversation_store = store
        self.home_paths = _Home(owner)
        self._capability_config_runtime_snapshot = type(
            "_S", (), {"config": config or CapabilityConfig()}
        )()
        self._current_run_params = type(
            "_P", (), {"task_attributes": {"conversation_thread_id": current_thread}}
        )()
        self.current_session_task_id = ""


def _params(**overrides):
    base = {"target_thread_id": "thread-b", "goal": "把 X 做好"}
    base.update(overrides)
    return base


def _agent(tmp_path, known, **kwargs):
    store = _FakeStore(tmp_path / "conv", known)
    return _Agent(store, **kwargs), store


def test_missing_target_is_structured_failure(tmp_path) -> None:
    agent, _ = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")})
    outcome = CreateSessionTaskTool(agent).execute(_params(target_thread_id=""))
    assert not outcome.ok
    assert outcome.error_code == "TOOL_PARAMETER_REQUIRED"


def test_unknown_target_out_of_scope(tmp_path) -> None:
    agent, store = _agent(tmp_path, {})
    outcome = CreateSessionTaskTool(agent).execute(_params(target_thread_id="thread-missing"))
    assert not outcome.ok
    assert outcome.error_code == SESSION_TARGET_OUT_OF_SCOPE
    assert store.guidance.calls == []


def test_im_target_rejected(tmp_path) -> None:
    agent, _ = _agent(tmp_path, {"thread-im": _FakeThread("thread-im", channel="feishu")})
    outcome = CreateSessionTaskTool(agent).execute(_params(target_thread_id="thread-im"))
    assert not outcome.ok
    assert outcome.error_code == SESSION_TARGET_CHANNEL_UNSUPPORTED


def test_task_switch_off_rejected(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_task_admin_enabled = False
    agent, _ = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert not outcome.ok
    assert outcome.error_code == "SESSION_TASK_NOT_ALLOWED"


def test_success_creates_record_and_queues_body(tmp_path) -> None:
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")})
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["status"] == "queued"
    assert payload["task_id"].startswith("stask-")
    # 正文只存一份：记录里只引用 guidance id。
    task = store.session_tasks.load(payload["task_id"])
    assert task.body_guidance_id == payload["body_guidance_id"] == "g-1"
    assert task.goal == "把 X 做好"
    request, _key = store.guidance.calls[0]
    assert request["metadata"]["origin_kind"] == "session_task"
    assert request["metadata"]["origin_thread_id"] == "thread-a"
    assert store.wakes.signals[0]["reason"] == "session_task"


def test_chain_limit_rejected(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_task_max_chain_depth = 1
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    root = store.session_tasks.create(
        sender_thread_id="X", target_thread_id="Y", goal="根", now=1.0
    )
    agent.current_session_task_id = root.task_id
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert not outcome.ok
    assert outcome.error_code == SESSION_TASK_CHAIN_LIMIT


def test_chain_limit_zero_means_unlimited(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_task_max_chain_depth = 0
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    root = store.session_tasks.create(sender_thread_id="X", target_thread_id="Y", goal="根", now=1.0)
    agent.current_session_task_id = root.task_id
    assert CreateSessionTaskTool(agent).execute(_params()).ok
