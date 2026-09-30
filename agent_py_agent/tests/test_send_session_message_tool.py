"""会话间发消息工具的合同测试（无真实模型请求）。

覆盖：工具注册进门控前、缺少参数的结构化失败、目标会话不存在时返回统一越界码
（不泄露存在性）、以及成功路径把来源结构化的消息幂等写入目标 guidance 队列。
用 fake 的最小 agent/store，不启动 Gateway，不调用模型。
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.send_session_message import (
    SendSessionMessageTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGE_KEY_FIELD,
    SESSION_TARGET_OUT_OF_SCOPE,
    SESSION_TASK_RATE_LIMIT,
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
    def __init__(self, status: str = "pending") -> None:
        self.calls: list[tuple[dict, str]] = []
        self.status = status

    def append_once(self, request: dict, *, dedupe_key: str):
        self.calls.append((request, dedupe_key))

        class _Entry:
            guidance_id = "guidance-fixed"

        return _Entry()

    # 函数用途: 按键返回一份回执，状态由测试指定（真实状态要原样回到工具结果里）。
    def receipt(self, dedupe_key: str):
        return SimpleNamespace(dedupe_key=dedupe_key, status=self.status)


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


class _FakeAgent:
    def __init__(self, store: _FakeStore, *, current_thread: str = "thread-a") -> None:
        self.conversation_store = store
        self.home_paths = _FakeHome()
        # 与生产同一入口：capability_config_for_agent 读运行时快照里的真 CapabilityConfig。
        self._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig())
        self._current_run_params = type(
            "_P", (), {"task_attributes": {"conversation_thread_id": current_thread}}
        )()


_OPERATIONS = itertools.count(1)


# 函数用途: 造一次工具调用的参数；执行器会注入本次调用的 operation_id，每次调用各不相同。
def _params(**overrides):
    base = {"target_thread_id": "thread-b", "message": "你好", "__operation_id": f"op-{next(_OPERATIONS)}"}
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


def _limited_store(tmp_path, limit: int, *, used: int = 0) -> _FakeStore:
    """给 fake store 挂上真实计数器，用来验证限额真的在工具入口生效。"""
    from agent_py_agent.agent.conversation import ConversationStore

    store = _FakeStore({"thread-b": _FakeThread("thread-b")})
    store.session_pair_rate = ConversationStore(tmp_path).session_pair_rate
    for _ in range(used):
        store.session_pair_rate.record("thread-a", "thread-b")
    agent_config_limit = limit
    return store, agent_config_limit


def test_pair_limit_blocks_send_and_does_not_queue(tmp_path) -> None:
    store, limit = _limited_store(tmp_path, 2, used=2)
    agent = _FakeAgent(store)
    agent._capability_config_runtime_snapshot.config.session_pair_hourly_limit = limit
    outcome = SendSessionMessageTool(agent).execute(_params())
    assert not outcome.ok
    assert outcome.error_code == SESSION_TASK_RATE_LIMIT
    # 被拒的消息不能投递，也不能占用配额。
    assert store.guidance.calls == []
    assert store.session_pair_rate.count("thread-a", "thread-b") == 2


def test_pair_limit_allows_until_limit_then_blocks(tmp_path) -> None:
    store, limit = _limited_store(tmp_path, 2, used=1)
    agent = _FakeAgent(store)
    agent._capability_config_runtime_snapshot.config.session_pair_hourly_limit = limit
    assert SendSessionMessageTool(agent).execute(_params()).ok is True
    assert store.session_pair_rate.count("thread-a", "thread-b") == 2
    blocked = SendSessionMessageTool(agent).execute(_params(message="第二条"))
    assert blocked.error_code == SESSION_TASK_RATE_LIMIT


def test_zero_limit_means_unlimited(tmp_path) -> None:
    store, _ = _limited_store(tmp_path, 0, used=50)
    agent = _FakeAgent(store)
    agent._capability_config_runtime_snapshot.config.session_pair_hourly_limit = 0
    assert SendSessionMessageTool(agent).execute(_params()).ok is True


def _agent_reading_file(store, config_path) -> _FakeAgent:
    """生产 Gateway 的形态：没有运行时快照，只按 capability_config_path 读文件。"""
    agent = _FakeAgent(store)
    agent._capability_config_runtime_snapshot = None
    agent.capability_config_path = config_path
    return agent


def test_reads_the_capability_file_not_an_agent_attribute(tmp_path) -> None:
    """旧实现读 agent.capability_config（生产上没有这个属性），文件里的限额永远不生效。"""
    config_path = tmp_path / "capability_config.yaml"
    config_path.write_text("session_pair_hourly_limit: 1\n", encoding="utf-8")
    store, _ = _limited_store(tmp_path, 1, used=1)
    outcome = SendSessionMessageTool(_agent_reading_file(store, config_path)).execute(_params())
    assert outcome.error_code == SESSION_TASK_RATE_LIMIT
    assert json.loads(outcome.output)["details"]["limit"] == 1


@pytest.mark.parametrize("content", [None, "decision_subagent_model_mode: bogus\n"], ids=["missing", "malformed"])
def test_unreadable_config_file_falls_back_to_default_pair_limit(tmp_path, content) -> None:
    """缺文件或文件损坏：每对每小时按 dataclass 默认 60 条，不能落到 0（不限制）。"""
    assert CapabilityConfig().session_pair_hourly_limit == 60
    config_path = tmp_path / "capability_config.yaml"
    if content is not None:
        config_path.write_text(content, encoding="utf-8")
    store, _ = _limited_store(tmp_path, 60, used=59)
    agent = _agent_reading_file(store, config_path)
    assert SendSessionMessageTool(agent).execute(_params()).ok is True
    blocked = SendSessionMessageTool(agent).execute(_params(message="第二条"))
    assert blocked.error_code == SESSION_TASK_RATE_LIMIT
    assert json.loads(blocked.output)["details"]["limit"] == 60


def test_each_call_gets_its_own_key_and_a_retry_reuses_it() -> None:
    """键按单条消息区分：两次调用（哪怕同样内容）各自一个键；同一次调用重试（operation_id 不变）用回同一个键。"""
    store = _FakeStore({"thread-b": _FakeThread("thread-b")})
    agent = _FakeAgent(store)
    first = _params()
    SendSessionMessageTool(agent).execute(first)
    SendSessionMessageTool(agent).execute(_params())
    SendSessionMessageTool(agent).execute(dict(first))
    keys = [key for _request, key in store.guidance.calls]
    assert keys[0] == f"session_message:thread-a->thread-b:{first['__operation_id']}"
    assert keys[0] != keys[1] and keys[2] == keys[0]
    assert [signal["metadata"][SESSION_MESSAGE_KEY_FIELD] for signal in store.wakes.signals] == keys
    assert [signal["dedupe_key"] for signal in store.wakes.signals] == keys, "同一条消息只发一条唤醒"


@pytest.mark.parametrize("status", ["pending", "consumed", "rejected"])
def test_result_reports_the_real_receipt_status(status) -> None:
    store = _FakeStore({"thread-b": _FakeThread("thread-b")})
    store.guidance.status = status
    payload = json.loads(SendSessionMessageTool(_FakeAgent(store)).execute(_params()).output)
    assert payload["status"] == status


def test_missing_operation_id_does_not_queue() -> None:
    """执行器没注入 operation_id 是宿主装配缺陷：不入队、不退回按会话对的旧键。"""
    store = _FakeStore({"thread-b": _FakeThread("thread-b")})
    outcome = SendSessionMessageTool(_FakeAgent(store)).execute(_params(__operation_id=""))
    assert (outcome.ok, outcome.effect_outcome) == (False, "not_started")
    assert store.guidance.calls == [] and store.wakes.signals == []
