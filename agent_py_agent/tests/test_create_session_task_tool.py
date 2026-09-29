"""create_session_task 工具的合同测试（无真实模型请求）。

覆盖：缺参数失败、目标不存在越界、权限拒绝、IM 目标拒绝、成功建记录 + 投正文 + 唤醒、
正文只存一份（body_guidance_id 指向 guidance）、链深超限拒绝。
用 fake 的最小 agent/store，不启动 Gateway，不调用模型。
"""

from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    CreateSessionTaskTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_TARGET_CHANNEL_UNSUPPORTED,
    SESSION_TARGET_OUT_OF_SCOPE,
    SESSION_TASK_CHAIN_LIMIT,
    SESSION_TASK_RATE_LIMIT,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft


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
        self.session_pair_rate = inner.session_pair_rate


class _Home:
    def __init__(self, owner):
        self.owner_provider, self.owner_kind, self.owner_id = owner


class _Agent:
    def __init__(self, store, *, owner=("local", "main", "main"), current_thread="thread-a", config=None,
                 session_task_id=""):
        self.conversation_store = store
        self.home_paths = _Home(owner)
        self._capability_config_runtime_snapshot = type(
            "_S", (), {"config": config or CapabilityConfig()}
        )()
        # 链来源只由宿主的结构化 task_attributes 提供（不设任何可被手工赋值的属性）。
        attributes = {"conversation_thread_id": current_thread}
        if session_task_id:
            attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = session_task_id
        self._current_run_params = type("_P", (), {"task_attributes": attributes})()


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
    """链来源由宿主的结构化 task_attributes 提供（与真实派活回合同一条路径）。"""
    config = CapabilityConfig()
    config.session_task_max_chain_depth = 1
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    root = store.session_tasks.create(
        SessionTaskDraft(
            sender_thread_id="X", target_thread_id="Y", goal="根"
        ), now=1.0,
    )
    agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = root.task_id
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert not outcome.ok
    assert outcome.error_code == SESSION_TASK_CHAIN_LIMIT


def test_chain_limit_zero_means_unlimited(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_task_max_chain_depth = 0
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    root = store.session_tasks.create(SessionTaskDraft(sender_thread_id="X", target_thread_id="Y", goal="根"), now=1.0)
    agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = root.task_id
    assert CreateSessionTaskTool(agent).execute(_params()).ok


def test_chain_depth_accumulates_across_hops(tmp_path) -> None:
    """A→B→C 真实累积：第二跳时链深已到 2，上限 2 就拒绝；上限 3 才放行。"""
    for limit, expected_ok in ((2, False), (3, True)):
        config = CapabilityConfig()
        config.session_task_max_chain_depth = limit
        agent, store = _agent(
            tmp_path / str(limit),
            {
                "thread-b": _FakeThread("thread-b"),
                "thread-c": _FakeThread("thread-c"),
                "thread-d": _FakeThread("thread-d"),
            },
            config=config,
        )
        first = store.session_tasks.create(
            SessionTaskDraft(sender_thread_id="thread-a", target_thread_id="thread-b", goal="第一跳"),
            now=1.0,
        )
        # 第二跳：b 在执行第一跳时再派出去，链来源就是第一跳的任务。
        agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = first.task_id
        second = store.session_tasks.create(
            SessionTaskDraft(
                sender_thread_id="thread-b", target_thread_id="thread-c", goal="第二跳",
                origin_task_id=first.task_id,
            ),
            now=2.0,
        )
        # 第三跳：c 在执行第二跳时再派，链深按 origin_task_id 链累计到 2。
        agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = second.task_id
        outcome = CreateSessionTaskTool(agent).execute(_params(target_thread_id="thread-d", goal="第三跳"))
        assert outcome.ok is expected_ok
        if expected_ok:
            assert json.loads(outcome.output)["status"] == "queued"
        else:
            assert outcome.error_code == SESSION_TASK_CHAIN_LIMIT


def test_pair_limit_blocks_dispatch_and_does_not_create_record(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_pair_hourly_limit = 1
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    store.session_pair_rate.record("thread-a", "thread-b")
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert not outcome.ok
    assert outcome.error_code == SESSION_TASK_RATE_LIMIT
    # 被拒的派活不能建记录、不能投正文。
    assert store.guidance.calls == []
    assert store.session_tasks.list_report(limit=0)[0] == []


def test_dispatch_records_into_pair_quota(tmp_path) -> None:
    config = CapabilityConfig()
    config.session_pair_hourly_limit = 1
    agent, store = _agent(tmp_path, {"thread-b": _FakeThread("thread-b")}, config=config)
    assert CreateSessionTaskTool(agent).execute(_params()).ok is True
    assert store.session_pair_rate.count("thread-a", "thread-b") == 1
    # 配额已被这次派活占满：下一次同样被拒。
    blocked = CreateSessionTaskTool(agent).execute(_params(goal="另一件事"))
    assert blocked.error_code == SESSION_TASK_RATE_LIMIT


def _agent_reading_file(tmp_path, known, config_path):
    """生产 Gateway 的形态：没有运行时快照，只按 capability_config_path 读文件。"""
    agent, store = _agent(tmp_path, known)
    agent._capability_config_runtime_snapshot = None
    agent.capability_config_path = config_path
    return agent, store


def _chain(store, length: int):
    """按 origin_task_id 串起 length 个任务，返回最后一个（它的链深是 length - 1）。"""
    task = None
    for hop in range(length):
        task = store.session_tasks.create(
            SessionTaskDraft(sender_thread_id=f"t{hop}", target_thread_id=f"t{hop + 1}", goal=f"第{hop + 1}跳",
                             origin_task_id=task.task_id if task else ""),
            now=float(hop + 1),
        )
    return task


@pytest.mark.parametrize("content", [None, "decision_subagent_model_mode: bogus\n"], ids=["missing", "malformed"])
def test_unreadable_config_file_enforces_default_chain_depth(tmp_path, content) -> None:
    """缺文件或文件损坏：链深按 dataclass 默认 4 生效，不能落到 0（不限制，防循环守卫失效）。"""
    assert CapabilityConfig().session_task_max_chain_depth == 4
    config_path = tmp_path / "capability_config.yaml"
    if content is not None:
        config_path.write_text(content, encoding="utf-8")
    known = {"thread-b": _FakeThread("thread-b")}
    agent, store = _agent_reading_file(tmp_path, known, config_path)
    # 从第 3 个任务再派：新任务链深 3，放行。
    agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = _chain(store, 3).task_id
    assert CreateSessionTaskTool(agent).execute(_params()).ok is True
    # 从第 4 个任务再派：新任务链深 4，达到默认上限，拒绝。
    agent._current_run_params.task_attributes[CONVERSATION_SESSION_TASK_ID_ATTR] = _chain(store, 4).task_id
    outcome = CreateSessionTaskTool(agent).execute(_params(goal="再往下派"))
    assert outcome.error_code == SESSION_TASK_CHAIN_LIMIT


@pytest.mark.parametrize("content", [None, "decision_subagent_model_mode: bogus\n"], ids=["missing", "malformed"])
def test_unreadable_config_file_falls_back_to_default_pair_limit(tmp_path, content) -> None:
    """缺文件或文件损坏：派活与发消息共用的每对每小时限额按默认 60 条，不能落到 0（不限制）。"""
    assert CapabilityConfig().session_pair_hourly_limit == 60
    config_path = tmp_path / "capability_config.yaml"
    if content is not None:
        config_path.write_text(content, encoding="utf-8")
    agent, store = _agent_reading_file(tmp_path, {"thread-b": _FakeThread("thread-b")}, config_path)
    for _ in range(60):
        store.session_pair_rate.record("thread-a", "thread-b")
    outcome = CreateSessionTaskTool(agent).execute(_params())
    assert outcome.error_code == SESSION_TASK_RATE_LIMIT
    assert json.loads(outcome.output)["details"]["limit"] == 60
