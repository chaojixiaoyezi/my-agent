from __future__ import annotations

import agent_py_agent.agent.conversation.store as conversation_store_module
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="主代理已看到事件并决定下一步。", backend=self.name)


def _thread(store: ConversationStore):
    return store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})


def _runtime(tmp_path, store: ConversationStore):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    return agent, backend, channels, BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})


def test_observation_and_wake_signal_are_durable_and_idempotent(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)

    observation = store.append_observation({'thread_id': thread.thread_id, 'event_type': "child_agent_event", 'summary': "孙代理发现需要主代理判断的异常。", 'urgency': "urgent", 'severity': "high", 'source_agent_id': "grandchild-1", 'parent_agent_id': "child-1", 'root_task_id': "task-1", 'evidence_refs': ["tool://log-query/1"], 'requires_main_agent': True, 'now': 20.0})
    signal = store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'reason': "urgent_child_event", 'dedupe_key': "task-1:urgent", 'now': 21.0})
    duplicate = store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'reason': "urgent_child_event", 'dedupe_key': "task-1:urgent", 'now': 22.0})

    assert duplicate.wake_signal_id == signal.wake_signal_id
    pending = store.pending_wake_signals()
    assert [item.wake_signal_id for item in pending] == [signal.wake_signal_id]
    assert pending[0].summary == "孙代理发现需要主代理判断的异常。"
    assert pending[0].evidence_refs == ("tool://log-query/1",)

    store.mark_wake_signal_handled(signal.wake_signal_id, now=30.0)

    assert store.pending_wake_signals() == []
    assert store.recent_observations(thread.thread_id, include_handled=False) == []
    assert store.recent_observations(thread.thread_id)[0].handled_at == 30.0


def test_combined_observation_wake_publishes_wake_first_and_links_both_sides(
    tmp_path, monkeypatch
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    original_append = conversation_store_module.append_jsonl
    pending_seen_before_observation = []

    def checked_append(path, payload, *, sort_keys=False):
        if "observations" in str(path):
            pending_seen_before_observation.extend(store.pending_wake_signals())
        return original_append(path, payload, sort_keys=sort_keys)

    monkeypatch.setattr(conversation_store_module, "append_jsonl", checked_append)
    observation, signal = store.append_observation_with_wake(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "子任务已完成。",
            "source_agent_id": "child-1",
            "root_task_id": "task-1",
            "requires_main_agent": True,
            "now": 20.0,
        },
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-1",
            "dedupe_key": "child-1:DONE",
            "metadata": {"task_id": "child-1", "status": "DONE"},
            "now": 20.1,
        },
    )

    assert [item.wake_signal_id for item in pending_seen_before_observation] == [
        signal.wake_signal_id
    ]
    assert observation.wake_signal_id == signal.wake_signal_id
    assert store.recent_observations(thread.thread_id)[0].wake_signal_id == signal.wake_signal_id
    store.mark_wake_signal_handled(signal.wake_signal_id, now=21.0)
    assert store.unhandled_observations_requiring_main() == []


def test_urgent_wake_signal_wakes_main_agent_without_due_policy(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期监控任务", 'now': 11.0})
    observation = store.append_observation({'thread_id': thread.thread_id, 'event_type': "runtime_alert", 'summary': "孙代理发现需要马上分析的异常日志。", 'urgency': "urgent", 'source_agent_id': "grandchild-1", 'root_task_id': "task-1", 'requires_main_agent': True, 'requires_llm_report': True, 'now': 20.0})
    signal = store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'reason': "urgent_runtime_alert", 'now': 20.1})
    _agent, backend, channels, scheduler = _runtime(tmp_path, store)

    reports = scheduler.tick(now=21.0)

    assert len(reports) == 1
    assert reports[0].reason == "urgent_runtime_alert"  # 修 reason 透传后:报告带 signal 真实 reason(非泛泛 urgent_wake_signal)
    assert store.pending_wake_signals() == []
    assert store.mark_wake_signal_handled(signal.wake_signal_id, now=22.0) is None
    assert "Pending Wake Signals" in backend.prompts[0]
    assert "孙代理发现需要马上分析的异常日志" in backend.prompts[0]
    assert channels.adapter("internal").sent_messages[0].content == "主代理已看到事件并决定下一步。"


def test_late_child_wake_for_superseded_root_is_archived_without_running(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-old", 'goal': "旧项目", 'now': 11.0})
    store.update_task_status({'task_id': "task-old", 'status': "superseded", 'now': 12.0})
    observation = store.append_observation({
        'thread_id': thread.thread_id,
        'event_type': "subagent_runner_finished",
        'summary': "旧项目的子代理迟到完成。",
        'source_agent_id': "subagent-old",
        'parent_agent_id': "task-old",
        'root_task_id': "task-old",
        'requires_main_agent': True,
        'now': 20.0,
    })
    store.raise_wake_signal({
        'thread_id': thread.thread_id,
        'observation': observation,
        'reason': "subagent_runner_finished",
        'now': 20.1,
    })
    _agent, backend, channels, scheduler = _runtime(tmp_path, store)

    reports = scheduler.tick(now=21.0)

    assert reports == []
    assert backend.prompts == []
    assert channels.adapter("internal").sent_messages == []
    assert store.pending_wake_signals() == []
    assert store.recent_observations(thread.thread_id)[0].handled_at == 21.0


def test_nonurgent_observation_requiring_main_agent_is_processed_on_next_tick(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.append_observation({'thread_id': thread.thread_id, 'event_type': "needs_review", 'summary': "子代理发现一个非紧急但需要主代理二次判断的现象。", 'urgency': "normal", 'source_agent_id': "child-1", 'requires_main_agent': True, 'now': 20.0})
    _agent, backend, _channels, scheduler = _runtime(tmp_path, store)

    reports = scheduler.tick(now=25.0)

    assert len(reports) == 1
    assert reports[0].reason == "observation_requires_main_agent"
    assert store.unhandled_observations_requiring_main() == []
    assert "Recent Observations" in backend.prompts[0]
    assert "非紧急但需要主代理二次判断" in backend.prompts[0]
