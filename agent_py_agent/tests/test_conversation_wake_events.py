from __future__ import annotations

import json

from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeChannelHub,
)
from agent_py_agent.agent.core import SimpleAgent


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
    channels = FakeChannelHub()
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


def test_urgent_wake_signal_wakes_main_agent_without_due_policy(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期监控任务", 'now': 11.0})
    observation = store.append_observation({'thread_id': thread.thread_id, 'event_type': "runtime_alert", 'summary': "孙代理发现需要马上分析的异常日志。", 'urgency': "urgent", 'source_agent_id': "grandchild-1", 'root_task_id': "task-1", 'requires_main_agent': True, 'requires_llm_report': True, 'now': 20.0})
    signal = store.raise_wake_signal({'thread_id': thread.thread_id, 'observation': observation, 'reason': "urgent_runtime_alert", 'now': 20.1})
    _agent, backend, channels, scheduler = _runtime(tmp_path, store)

    reports = scheduler.tick(now=21.0)

    assert len(reports) == 1
    assert reports[0].reason == "urgent_wake_signal"
    assert store.pending_wake_signals() == []
    assert store.mark_wake_signal_handled(signal.wake_signal_id, now=22.0) is None
    assert "Pending Wake Signals" in backend.prompts[0]
    assert "孙代理发现需要马上分析的异常日志" in backend.prompts[0]
    assert channels.adapter("internal").sent_messages[0].content == "主代理已看到事件并决定下一步。"


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


def test_raise_event_tool_resolves_thread_from_task(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = _thread(store)
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "监控任务", 'now': 11.0})
    result = RaiseEventTool(agent).execute(
        {
            "task_id": "task-1",
            "event_type": "runtime_alert",
            "summary": "孙代理发现紧急事件，需要主代理立刻处理。",
            "source_agent_id": "grandchild-1",
            "urgency": "urgent",
            "evidence_refs": ["tool://event/1"],
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["thread_id"] == thread.thread_id
    assert payload["wake_signal_id"].startswith("wake-")
    assert store.pending_wake_signals()[0].summary == "孙代理发现紧急事件，需要主代理立刻处理。"


def test_raise_event_thread_binding_error_is_structured(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    def broken_thread_for_task(task_id):
        del task_id
        raise ValueError("task binding index broken")

    monkeypatch.setattr(agent.conversation_store, "thread_for_task", broken_thread_for_task)

    result = RaiseEventTool(agent).execute({"task_id": "task-1", "summary": "需要主代理处理。"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "task_thread_lookup_failed"
    assert payload["load_error"]["context"] == "raise_event.thread_for_task"
    assert payload["load_error"]["category"] == "data_parse"


def test_raise_event_reports_corrupt_explicit_thread(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = _thread(agent.conversation_store)
    agent.conversation_store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")

    result = RaiseEventTool(agent).execute({"thread_id": thread.thread_id, "summary": "需要主代理处理。"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "thread_lookup_failed"
    assert payload["load_error"]["context"] == "raise_event.load_thread"
    assert payload["load_error"]["thread_id"] == thread.thread_id


def test_raise_event_subagent_task_load_error_is_structured(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    def broken_load(task_id):
        del task_id
        raise OSError("subagent ledger missing")

    monkeypatch.setattr(agent.subagents, "load", broken_load)

    result = RaiseEventTool(agent).execute({"task_id": "child-1", "summary": "需要主代理处理。"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "subagent_task_load_failed"
    assert payload["load_error"]["context"] == "raise_event.subagents.load"
    assert payload["load_error"]["category"] == "io"


def test_raise_event_lineage_load_error_stays_with_observation(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = _thread(agent.conversation_store)
    agent.conversation_store.bind_task(
        {'thread_id': thread.thread_id, 'task_id': "child-1", 'goal': "监控任务", 'now': 11.0}
    )

    def broken_load(task_id):
        del task_id
        raise OSError("lineage ledger missing")

    monkeypatch.setattr(agent.subagents, "load", broken_load)

    result = RaiseEventTool(agent).execute({"task_id": "child-1", "summary": "需要主代理处理。"})
    payload = json.loads(result.output)
    observation = agent.conversation_store.recent_observations(thread.thread_id)[-1]

    assert result.ok is True
    assert payload["observation_id"].startswith("obs-")
    assert observation.source_agent_id == "child-1"
    assert observation.root_task_id == "child-1"
    assert observation.metadata["lineage_load_error"]["context"] == "raise_event.lineage.subagents.load"
    assert observation.metadata["lineage_load_error"]["category"] == "io"


def test_raise_event_wake_failure_is_reported_without_losing_observation(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = _thread(agent.conversation_store)

    def broken_wake(payload):
        del payload
        raise OSError("wake ledger locked")

    monkeypatch.setattr(agent.conversation_store, "raise_wake_signal", broken_wake)

    result = RaiseEventTool(agent).execute(
        {
            "thread_id": thread.thread_id,
            "event_type": "runtime_alert",
            "summary": "孙代理发现紧急事件，需要主代理立刻处理。",
            "urgency": "urgent",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["observation_id"].startswith("obs-")
    assert payload["wake_signal_id"] == ""
    assert payload["wake_signal_error"]["context"] == "raise_event.raise_wake_signal"
    assert agent.conversation_store.recent_observations(thread.thread_id)[-1].summary == "孙代理发现紧急事件，需要主代理立刻处理。"


def test_main_event_tools_are_registered_for_subagent_contexts(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    registered = {spec.name for spec in agent.tools.specs(include_orchestration=True)}

    assert "raise_event" in registered
    assert "raise_event" in registered
