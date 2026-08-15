from __future__ import annotations

import json

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
    signal = store.pending_wake_signals()[0]
    observation = store.recent_observations(thread.thread_id)[-1]
    assert signal.summary == "孙代理发现紧急事件，需要主代理立刻处理。"
    assert signal.observation_id == observation.observation_id
    assert observation.wake_signal_id == signal.wake_signal_id


def test_raise_event_atomic_pair_runs_one_background_turn_only(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "持续监控",
            "now": 11.0,
        }
    )
    agent, backend, channels, scheduler = _runtime(tmp_path, store)

    result = RaiseEventTool(agent).execute(
        {
            "task_id": "task-1",
            "source_agent_id": "child-1",
            "event_type": "tool_failure",
            "summary": "子代理需要主代理处理一次失败。",
            "urgency": "urgent",
            "requires_main_agent": True,
        }
    )

    first = scheduler.tick(now=21.0)
    second = scheduler.tick(now=22.0)

    assert result.ok is True
    assert len(first) == 1
    assert first[0].reason == "tool_failure"
    assert second == []
    assert len(backend.prompts) == 1
    assert len(channels.adapter("internal").sent_messages) == 1
    assert store.pending_wake_signals() == []
    assert store.unhandled_observations_requiring_main() == []


def test_root_agent_urgent_event_is_recorded_without_self_wake(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = _thread(store)
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "持续研判",
            "now": 11.0,
        }
    )

    result = RaiseEventTool(agent).execute(
        {
            "task_id": "task-root",
            "root_task_id": "task-root",
            "source_agent_id": "task-root",
            "event_type": "critical_security_event",
            "summary": "根代理已经在处理的同一事件。",
            "urgency": "urgent",
            "requires_main_agent": True,
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["observation_id"].startswith("obs-")
    assert payload["wake_signal_id"] == ""
    assert store.pending_wake_signals() == []
    assert store.recent_observations(thread.thread_id)[-1].summary == (
        "根代理已经在处理的同一事件。"
    )


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
    # 运行时 thread_for_task 抛错是可恢复执行失败(可重试)，不是"参数不合法"——
    # 以前硬编码 TOOL_INVALID_ARGUMENTS 会把模型引去反复改参数(ntu-stage2 审计修复)。
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.retryable is True
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


def test_raise_event_uses_structured_source_agent_for_lineage(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = _thread(agent.conversation_store)
    agent.conversation_store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "req-root", "goal": "根任务", "now": 11.0}
    )
    child = SimpleNamespace(id="subagent-child", parent_id="req-root", root_id="req-root")

    def load_source_agent(run_id):
        if run_id == child.id:
            return child
        raise FileNotFoundError(run_id)

    monkeypatch.setattr(agent.subagents, "load", load_source_agent)

    result = RaiseEventTool(agent).execute(
        {
            "task_id": "req-root",
            "source_agent_id": child.id,
            "summary": "子代理完成。",
        }
    )
    observation = agent.conversation_store.recent_observations(thread.thread_id)[-1]

    assert result.ok is True
    assert observation.source_agent_id == child.id
    assert observation.parent_agent_id == "req-root"
    assert observation.root_task_id == "req-root"
    assert "lineage_load_error" not in observation.metadata


def test_raise_event_wake_failure_is_reported_without_losing_observation(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import RaiseEventTool

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = _thread(agent.conversation_store)

    original_append = agent.conversation_store.append_observation

    def broken_atomic(observation_payload, wake_payload):
        del wake_payload
        original_append(observation_payload)
        raise OSError("wake ledger locked")

    monkeypatch.setattr(
        agent.conversation_store,
        "append_observation_with_wake",
        broken_atomic,
    )

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
    assert payload["wake_signal_error"]["context"] == (
        "raise_event.append_observation_with_wake"
    )
    observation = agent.conversation_store.recent_observations(thread.thread_id)[-1]
    assert observation.observation_id == payload["observation_id"]
    assert observation.wake_signal_id == ""
    assert observation.summary == "孙代理发现紧急事件，需要主代理立刻处理。"


def test_main_event_tools_are_registered_for_subagent_contexts(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    registered = {spec.name for spec in agent.tools.specs(include_orchestration=True)}

    assert "raise_event" in registered
    assert "raise_event" in registered
