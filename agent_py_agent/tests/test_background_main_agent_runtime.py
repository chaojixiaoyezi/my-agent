from __future__ import annotations

import json
import time

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderUsageLimitError
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig


def test_background_run_params_carry_structured_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(
        thread_id="thread-1",
        task_id="task-1",
        reason="scheduled_progress_report",
    )

    params = _run_params(request.thread_id, request)

    assert params.source == "background_main_agent"
    assert params.task_attributes == {
        "conversation_thread_id": "thread-1",
        "conversation_task_id": "task-1",
    }


def test_background_run_without_task_does_not_invent_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(thread_id="thread-chat", task_id="", reason="observation_batch")

    params = _run_params(request.thread_id, request)

    assert params.task_attributes is None


def test_background_run_restores_authoritative_task_workspace_and_title(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    workspace = tmp_path / "task-library"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-library",
            "goal": "图书馆运营方案",
            "task_path": str(workspace),
            "now": 11.0,
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-library"),
        agent,
    )

    assert params.task_attributes["task_title"] == "图书馆运营方案"
    assert params.task_attributes["run_workspace"] == {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="后台主代理已检查任务树，并给出阶段汇报。", backend=self.name)


class _NaturalCompletionBackend:
    name = "natural-completion"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="任务全部完成。", backend=self.name)


class _CollaborationRehearsalBackend:
    name = "collaboration-rehearsal"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert self.case_id in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "artifact://source-a/e1" in prompt
            assert "artifact://source-b/e2" in prompt
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"inspect_agent_tree"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert '"does_not_dispatch": true' in prompt
        return ModelResponse(text="协作演练完成：已读取 case 状态和代理树。", backend=self.name)


class _BlockedCollaborationBackend:
    name = "blocked-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert "不可达=1" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "collection_result" in prompt
        assert "ready_to_report" in prompt
        return ModelResponse(text="协作阻塞已确认：需要主代理调整策略。", backend=self.name)


class _PlainLanguageCollaborationBackend:
    name = "plain-language-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "帮我协调几个后台代理，有阻塞就继续安排或告诉我" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "collection_result" in prompt
            assert "ready_to_report" in prompt
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n'
                    f'{{"tool":"update_collaboration","case_id":"{self.case_id}",'
                    '"status":"needs_replan","summary":"已看到阻塞请求，下一步需要换来源或补派代理。"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "needs_replan" in prompt
        return ModelResponse(text="我已经看到阻塞点，会换来源或补派代理继续推进。", backend=self.name)


class _SlowBackend:
    name = "slow"

    def __init__(self, *, sleep_seconds: float):
        self.sleep_seconds = sleep_seconds

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        time.sleep(self.sleep_seconds)
        return ModelResponse(text="后台主代理慢速检查完成。", backend=self.name)


class _FailingBackend:
    name = "failing"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise RuntimeError("backend boom")


class _GoalToolProgressBackend:
    name = "goal-tool-progress"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="本轮已经根据目录事实继续推进。", backend=self.name)


class _GoalCompletingBackend:
    name = "goal-completing"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"update_goal","status":"complete"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="已经完成整合和验证。", backend=self.name)


class _MidTurnLifecycleBackend:
    name = "mid-turn-lifecycle"

    def __init__(self, *, store, thread_id: str, task_id: str, fail_after_injection: bool = False):
        self.store = store
        self.thread_id = thread_id
        self.task_id = task_id
        self.fail_after_injection = fail_after_injection
        self.calls = 0
        self.prompts: list[str] = []
        self.signal = None

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            self.signal = self.store.raise_wake_signal(
                {
                    "thread_id": self.thread_id,
                    "root_task_id": self.task_id,
                    "reason": "subagent_runner_finished",
                    "source_agent_id": "child-mid-turn",
                    "metadata": {"task_id": "child-mid-turn", "status": "DONE"},
                    "now": 20.5,
                }
            )
            return ModelResponse(text="这是子代理完成前生成的旧状态。", backend=self.name)
        assert "[RUNTIME_TASK_EVENTS]" in prompt
        assert "child-mid-turn" in prompt
        if self.fail_after_injection:
            raise RuntimeError("provider failed after runtime event injection")
        return ModelResponse(text="已接收子代理的新结果并继续整合。", backend=self.name)


class _InternalStatusBackend:
    name = "internal-status"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        return ModelResponse(
            text=(
                '[RUN_TOOL_EVIDENCE_BLOCKED]\n'
                '{"reason":"scheduled_progress_report","private":"must-not-enter-chat"}'
            ),
            backend=self.name,
        )


def test_background_runtime_reports_corrupt_thread_before_running_model(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    try:
        runtime.run_once({"thread_id": thread.thread_id, "reason": "scheduled_progress_report"})
    except DataCorruptionError as exc:
        assert "conversation.thread.read" in str(exc)
        assert thread.thread_id in str(exc)
    else:
        raise AssertionError("corrupt thread should be reported as data corruption")


def test_due_progress_policy_wakes_background_main_agent_and_sends_message(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})

    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "长期后台任务", 'now': 10.0})
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "每小时帮我看一次进展，有问题就调度。",
        'channel': "internal",
        'metadata': {"gateway_request_id": "task-1"},
        'now': 11.0,
    })
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "观察子代理任务树", 'now': 12.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 13.0})

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "每小时帮我看一次进展" in backend.prompts[0]
    assert "inspect_agent_tree" in backend.prompts[0]
    assert "dispatch_subagents" in backend.prompts[0]
    assert "create_subagents" not in backend.prompts[0]
    sent = channels.adapter("internal").sent_messages
    assert sent[0].target == "thread-1"
    assert "后台主代理已检查任务树" in sent[0].content


def test_thread_goal_turn_with_no_tool_calls_stops_auto_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    goal = store.create_goal(
        {"thread_id": thread.thread_id, "objective": "持续推进同一件工作", "now": 11.0}
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "now": 12.0,
        }
    )
    first_wake = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=14.0)

    assert len(reports) == 1
    assert "Continue working toward the active thread goal" in backend.prompts[0]
    updated = store.load_goal(thread.thread_id)
    assert updated is not None and updated.status == "active"
    pending = store.pending_wake_signals()
    assert pending == []
    assert first_wake.status == "pending"


def test_thread_goal_with_tool_progress_schedules_exactly_one_next_turn(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _GoalToolProgressBackend()
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-progress",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal(
        {"thread_id": thread.thread_id, "objective": "持续检查目录并推进"}
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    first = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    reports = scheduler.tick()

    assert len(reports) == 1 and reports[0].tool_call_count == 1
    pending = store.pending_wake_signals()
    assert len(pending) == 1
    assert pending[0].wake_signal_id != first.wake_signal_id
    assert pending[0].reason == "thread_goal_continue"


def test_thread_goal_waits_for_child_events_without_polling_or_chat_noise(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    backend = _GoalToolProgressBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-child",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "完成一个并行项目"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    agent.subagents.create_run(
        goal="实现模块甲",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    first = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id=goal.task_id,
            reason="thread_goal_continue",
        ),
        agent,
    )
    reports = scheduler.tick()

    assert "wait" not in params.allowed_tools
    assert "create_subagents" not in params.allowed_tools
    assert reports == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []
    assert first.status == "pending"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []

    guidance = store.append_guidance(
        {
            "target_type": "task",
            "target_id": goal.task_id,
            "message": "补充一个当前任务要求",
            "sender": "user",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal-guided:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id, "guidance_id": guidance.guidance_id},
        }
    )

    guided_reports = scheduler.tick()

    assert len(guided_reports) == 1
    assert guided_reports[0].delivery_status == "suppressed"
    assert "Their lifecycle events will wake this same goal again" in backend.prompts[0]
    assert store.pending_wake_signals() == []


def test_terminal_goal_children_trigger_one_integrating_closeout(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    backend = _GoalCompletingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-closeout",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "完成并验证整个项目"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    child = agent.subagents.create_run(
        goal="完成实现",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )

    reports = scheduler.tick(now=signal.created_at + 100)

    assert len(reports) == 1
    assert reports[0].delivery_status == "sent"
    assert reports[0].delivery_reason == "thread_goal_completion"
    assert "Completion audit" in backend.prompts[0]
    assert "待你验证的材料" in backend.prompts[0]
    assert store.load_goal(thread.thread_id).status == "complete"
    links = {item.task_id: item for item in store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "completed"
    assert [item.content for item in channels.adapter("internal").sent_messages] == [
        "已经完成整合和验证。"
    ]

    stale = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child-stale:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )
    assert scheduler._run_wake_signal(stale, now=time.time()) is None
    assert backend.calls == 2
    assert signal.status == "pending"


def test_thread_goal_provider_usage_limit_maps_to_usage_limited(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-limit",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "持续推进"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    def fail_run(_request):
        raise ProviderUsageLimitError("HTTP 429")

    monkeypatch.setattr(scheduler, "_run_claimed", fail_run)
    with pytest.raises(ProviderUsageLimitError):
        scheduler._run_wake_signal(signal, now=20.0)

    updated = store.load_goal(thread.thread_id)
    assert updated is not None and updated.status == "usage_limited"
    links = {item.task_id: item for item in store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"
    assert store.pending_wake_signals() == []


def test_completed_task_drops_queued_scheduled_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "foreground-terminal-wake",
            "channel_user_id": "user-1",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-complete",
            "goal": "完成长任务",
            "status": "active",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-complete",
            "reason": "scheduled_progress_report",
            "dedupe_key": "foreground-task-complete",
        }
    )
    store.update_task_status({"task_id": "task-complete", "status": "completed"})

    assert scheduler._run_wake_signal(signal, now=time.time()) is None
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_task_continuation_uses_the_same_thread_history_and_compact(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({
        'canonical_user_id': "user-1",
        'channel': "feishu",
        'channel_conversation_id': "chat-1",
        'channel_user_id': "user-1",
        'now': 10.0,
    })
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "请完成任务甲的七天晚餐方案。",
        'channel': "feishu",
        'metadata': {"gateway_request_id": "task-1"},
        'now': 11.0,
    })
    store.bind_task({
        'thread_id': thread.thread_id,
        'task_id': "task-1",
        'goal': "完成任务甲的七天晚餐方案",
        'now': 12.0,
    })
    store.bind_task({
        'thread_id': thread.thread_id,
        'task_id': "task-2",
        'goal': "任务乙私有目标-不应出现在任务甲",
        'now': 13.0,
    })
    store.update_summary(thread.thread_id, "普通聊天压缩摘要-青柚47", now=14.0)
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "普通聊天核对词青柚47，不要把它写进任务。",
        'channel': "feishu",
        'metadata': {"gateway_request_id": "chat-request-2"},
        'now': 15.0,
    })
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "预算控制在三百元内。",
        'channel': "feishu",
        'metadata': {"kind": "active_turn_user_input"},
        'now': 16.0,
    })
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "第二步只实现营养评分、时间衰减和对应测试。",
        'channel': "feishu",
        'metadata': {"gateway_request_id": "chat-request-3"},
        'now': 16.75,
    })
    store.set_progress_policy({
        'thread_id': thread.thread_id,
        'task_id': "task-1",
        'interval_seconds': 60,
        'route_channel': "feishu",
        'route_target': "chat-1",
        'now': 17.0,
    })

    reports = scheduler.tick(now=77.0)
    prompt = backend.prompts[0]

    assert len(reports) == 1
    assert "请完成任务甲的七天晚餐方案" in prompt
    assert "完成任务甲的七天晚餐方案" in prompt
    assert "预算控制在三百元内" in prompt
    assert "第二步只实现营养评分、时间衰减和对应测试" in prompt
    assert "青柚47" in prompt
    assert "普通聊天压缩摘要-青柚47" in prompt
    assert "任务乙私有目标" in prompt
    assert '"ordinary_thread_messages_included": false' not in prompt
    assert '"conversation_compact_included": false' not in prompt


def test_automatic_supervision_skips_unchanged_llm_turn_and_runs_on_material_delta(tmp_path) -> None:
    from agent_py_agent.agent.conversation.progress_fingerprint import subagent_material_signature

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="后台做长任务",
        thought="",
        plan=["执行"],
        parent_id="task-1",
        root_id="task-1",
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "后台做长任务",
            "now": 11.0,
        }
    )
    signature = subagent_material_signature(
        agent,
        task_id="task-1",
        watched_run_ids=[child.id],
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "now": 12.0,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "watched_run_ids": [child.id],
                "material_signature": signature,
            },
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=73.0) == []
    assert backend.prompts == []
    checked = store.get_progress_policy(policy.policy_id)
    assert checked is not None
    assert checked.last_report_at == 0.0
    assert checked.metadata["last_material_check_at"] == 73.0

    changed = agent.subagents.load(child.id)
    changed.progress = 0.5
    changed.last_progress_at = 80.0
    changed.last_progress_summary = "完成一半"
    agent.subagents.save(changed)

    reports = scheduler.tick(now=checked.next_due_at + 1)

    assert len(reports) == 1
    assert len(backend.prompts) == 2
    assert "[natural-user-reply]" not in backend.prompts[0]
    assert "[natural-user-reply]" in backend.prompts[1]
    updated = store.get_progress_policy(policy.policy_id)
    assert updated is not None
    assert updated.metadata["material_signature"] != signature


def test_partial_successful_subagent_wake_stays_out_of_ordinary_chat_until_batch_finishes(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    second = agent.subagents.create_run(
        goal="完成第二部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "RUNNING")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    partial = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert partial.delivery_status == "suppressed"
    assert partial.delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []

    agent.backend = _NaturalCompletionBackend()
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    final = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": second.id,
                "metadata": {"task_id": second.id, "status": "DONE"},
            },
            "now": 30.0,
        }
    )

    assert final.delivery_status == "sent"
    assert final.delivery_reason == "root_subagents_terminal"
    assert len(channels.adapter("internal").sent_messages) == 1
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [final.response]


def test_subagent_state_load_error_suppresses_until_exact_root_task_is_completed(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="完成当前部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    unrelated = agent.subagents.workspace / "unrelated-broken-run"
    unrelated.mkdir(parents=True)
    (unrelated / "task.json").write_text("{ broken", encoding="utf-8")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "完成全部工作", "now": 11.0}
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason="subagent_runner_finished",
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": child.id,
            "metadata": {"task_id": child.id, "status": "DONE"},
        },
    )

    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is False
    assert reason == "subagent_state_load_error"

    store.update_task_status({"task_id": "task-root", "status": "completed", "now": 20.0})
    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is True
    assert reason == "root_task_completed_with_subagent_state_load_error"


def test_completion_observation_fallback_uses_same_partial_delivery_policy(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.create_run(
        goal="完成第二部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "第一部分已完成。",
            "source_agent_id": first.id,
            "root_task_id": "task-root",
            "requires_main_agent": True,
            "metadata": {"task_id": first.id, "status": "DONE"},
            "now": 20.0,
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
            "store": store,
        }
    )

    reports = scheduler.tick(now=21.0)

    assert len(reports) == 1
    assert reports[0].reason == "subagent_runner_finished"
    assert reports[0].delivery_status == "suppressed"
    assert reports[0].delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []


def test_internal_wait_continuation_stays_out_of_chat_while_child_runs(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.delivery_reason == "internal_scheduled_continuation"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []


def test_internal_wait_completion_delivers_model_authored_final_reply(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [report.response]


def test_scheduled_turn_accepts_mid_turn_child_event_without_starting_second_main_run(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=72.0)

    assert len(reports) == 1
    assert backend.calls == 2
    assert "RUNTIME_TASK_EVENTS" not in backend.prompts[0]
    assert backend.signal is not None
    assert backend.signal.wake_signal_id in backend.prompts[1]
    assert reports[0].response == "已接收子代理的新结果并继续整合。"
    assert store.pending_wake_signals() == []
    claim = store.load_background_run_claim(thread.thread_id)
    assert claim["status"] == "finished"


def test_mid_turn_child_event_stays_retryable_when_provider_fails_after_injection(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
        fail_after_injection=True,
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    try:
        scheduler.tick(now=72.0)
    except RuntimeError as exc:
        assert "provider failed after runtime event injection" in str(exc)
    else:
        raise AssertionError("provider failure should leave the runtime event retryable")

    assert backend.signal is not None
    assert [item.wake_signal_id for item in store.pending_wake_signals()] == [
        backend.signal.wake_signal_id
    ]
    assert store.load_background_run_claim(thread.thread_id)["status"] == "failed"


def test_internal_continuation_delivers_natural_runtime_completion(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    agent.backend = _NaturalCompletionBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    messages = store.recent_messages(thread.thread_id)
    assert [row.content for row in messages] == ["任务全部完成。"]
    assert messages[0].created_at > 20.0


def test_done_child_wake_delivers_natural_final_response(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child.id,
                "metadata": {"task_id": child.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "root_subagents_terminal"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [report.response]


def test_successful_sibling_completion_wakes_are_coalesced_before_one_llm_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=5,
        ),
        tmp_path,
    )
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    for goal in ("第一部分", "第二部分"):
        child = agent.subagents.create_run(
            goal=goal,
            thought="",
            plan=["执行"],
            parent_id="task-root",
            root_id="task-root",
        )
        agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "两路并行", "now": 11.0}
    )
    for index, created_at in enumerate((20.0, 21.0), start=1):
        store.raise_wake_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "subagent_runner_finished",
                "root_task_id": "task-root",
                "source_agent_id": f"child-{index}",
                "metadata": {"task_id": f"child-{index}", "status": "DONE"},
                "now": created_at,
            }
        )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=23.0) == []
    assert backend.prompts == []
    assert len(store.pending_wake_signals()) == 2

    reports = scheduler.tick(now=26.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert store.pending_wake_signals() == []
    assert reports[0].delivery_status == "sent"


def test_failed_subagent_completion_wake_is_not_delayed_by_success_coalescing(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=30,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "失败立即处理", "now": 11.0}
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-root",
            "source_agent_id": "child-failed",
            "metadata": {"task_id": "child-failed", "status": "FAILED"},
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    reports = scheduler.tick(now=20.1)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert reports[0].delivery_reason == "subagent_non_success_terminal"


def test_background_internal_status_is_not_saved_as_ordinary_chat(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _InternalStatusBackend()
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
            "now": 10.0,
        }
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "scheduled_progress_report",
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 20.0,
        }
    )

    assert report.response == ""
    assert report.delivery_status == "suppressed"
    assert store.recent_messages(thread.thread_id, limit=1) == []
    assert channels.adapter("feishu").sent_messages == []


def test_scheduler_records_bad_progress_policy_without_blocking_due_policy(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})

    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 13.0})
    bad_path = store.policies_dir / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert scheduler.last_progress_policy_load_errors
    assert scheduler.last_progress_policy_load_errors[0]["context"] == "conversation.progress_policy.read"
    assert scheduler.last_progress_policy_load_errors[0]["policy_id"] == "broken"
    assert channels.adapter("internal").sent_messages


def test_scheduler_retires_stale_missed_progress_policy_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "陈年提醒退休不复活", 'now': 11.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})

    reports = scheduler.tick(now=12.0 + 7200 + 61)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    # 早已超出 catchup 宽限(>2h)的 stale 策略应被退休(enabled=False),不再续命。
    # 旧行为 mark_progress_reported 把 next_due 重置成 now+interval,下个间隔又变 runnable 发 LLM
    # 进度汇报,无限 churn 占满 gateway worker。退休=从 due 扫描里彻底消失。
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_renews_stale_policy_while_coverage_open(tmp_path) -> None:
    """g8 问题B·stale 不杀活任务:任务清单还有未闭环项时,错过追赶窗(唤醒轮长期领不到
    claim/网关中断)只把排期推进到下一 interval 继续追,不许永久退休——账没对完唤醒链不许死。
    无清单的 stale(上一测试)仍照旧退休,churn 防护不变。"""
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "活任务的续推提醒", 'now': 11.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    write_task_progress(
        runtime_owner_root(agent), "task-1",
        {"coverage": {"targets": [{"id": "req-01", "title": "模块1", "status": "pending"}]}},
    )
    stale_now = 12.0 + 7200 + 61

    reports = scheduler.tick(now=stale_now)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    renewed = store.get_progress_policy(policy.policy_id)
    assert renewed is not None and renewed.enabled is True, "清单未闭环的 stale 提醒只续命不退休"
    assert renewed.next_due_at > stale_now, "排期推进到下一 interval,下轮照常追"


@pytest.mark.parametrize("terminal_status", ["DONE", "completed", "superseded"])
def test_scheduler_retires_terminal_task_progress_policy_without_model_call(
    tmp_path, terminal_status
) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "终态任务退休watch", 'now': 11.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    store.update_task_status({'task_id': "task-1", 'status': terminal_status, 'now': 70.0})

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "terminal_task_link"
    # 被观察任务已终态时，watch 策略应退休(enabled=False),不再每个间隔唤醒后台主代理发
    # LLM 进度汇报(churn 根因)。这里 now=100 未到 stale 窗口,确保抑制原因是终态而非陈旧。
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_terminal_watch_policy_stays_runnable_while_watch_backlog_open(tmp_path) -> None:
    # P1 消费吞吐:盯守子代理 turn 结束进 DONE(常态)→ link 终态;若按终态一刀切抑制,
    # 盯守 policy 下一拍就被退休、永不 fire,唤醒链每次 DONE 都自埋——owner 还有未清账
    # 盯守路(spool 有已抬未判完候选)时,盯守类 policy 必须照常 runnable;账清后照常退休。
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _runnable_due_policies
    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({
        'canonical_user_id': "user-1", 'channel': "internal",
        'channel_conversation_id': "conv:run-w", 'channel_user_id': "user-1", 'now': 10.0,
    })
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "run-w", 'goal': "盯守", 'now': 11.0})
    policy = store.set_progress_policy({
        'thread_id': thread.thread_id, 'task_id': "run-w", 'interval_seconds': 60,
        'route_channel': "internal", 'route_target': "",
        'metadata': {"kind": "subagent_progress_watch", "tool": "wait", "watch_run_id": "run-w"},
        'now': 12.0,
    })
    store.update_task_status({'task_id': "run-w", 'status': "DONE", 'now': 70.0})
    owner_home = tmp_path / "owner"
    lane = new_state(owner_home, "http://127.0.0.1:9/pull", {"watch_window_seconds": 600})
    lane.opened_at = time.time() - 900.0  # 窗口已走完
    lane.totals["spool_candidates"] = 7  # 已抬 7 条、无人 ack = 未清账
    persist_state(lane)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))

    # now 用真实时钟:owner_has_incomplete_watch 拿它对 lane 的 opened_at 算窗口。
    runnable, suppressed = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert [p.policy_id for p in runnable] == [policy.policy_id], "盯守未清账时终态不该埋掉唤醒链"
    assert suppressed == []

    # 账清(ack 追平写入)后:同一 policy 照常按终态退休,豁免有终点。
    sidecar = state_dir(owner_home) / f"{lane.watch_id}.read.json"
    sidecar.write_text(
        json.dumps({"read_seq": 9, "candidates_consumed": 7, "candidates_acked": 7, "updated_at": time.time()}),
        encoding="utf-8",
    )
    runnable2, suppressed2 = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert runnable2 == []
    assert [reason for _p, reason in suppressed2] == ["terminal_task_link"]


def test_due_policy_backs_off_on_no_progress_rounds_and_recovers(tmp_path) -> None:
    # §6-B4 退避钉子:唤醒轮【零成功工具调用】(卡死空转,真机=BLOCKED 子代理让主代理每分钟
    # 醒来空转解阻、饿死并发建站用户)→ 间隔按 2^streak 拉长、封顶 8×,让出调度资源但永不
    # 停机;一有成功工具调用立即归零复原。判据全结构化(tool_success_count),不做文本判断。
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({
        'canonical_user_id': "user-1", 'channel': "internal",
        'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 0.0,
    })
    policy = store.set_progress_policy({
        'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60,
        'route_channel': "internal", 'route_target': "thread-1", 'now': 0.0,
    })

    class _FakeRuntime:
        agent = None

        def __init__(self) -> None:
            self.tool_success_count = 0

        def run_once(self, params: dict) -> BackgroundMainAgentReport:
            return BackgroundMainAgentReport(
                thread_id=str(params.get("thread_id") or ""),
                task_id=str(params.get("task_id") or ""),
                reason=str(params.get("reason") or ""),
                response="轮次完成",
                route_channel="internal",
                route_target="thread-1",
                created_at=float(params.get("now") or 0.0),
                tool_call_count=2,
                tool_success_count=self.tool_success_count,
            )

    runtime = _FakeRuntime()
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})

    # 第 1 轮无进展:streak=1 → 间隔 60 → 120
    scheduler.tick(now=61.0)
    after_first = store.get_progress_policy(policy.policy_id)
    assert after_first.metadata["no_progress_streak"] == 1
    assert after_first.next_due_at == 61.0 + 120

    # 第 2 轮无进展:streak=2 → ×4
    scheduler.tick(now=after_first.next_due_at + 1)
    after_second = store.get_progress_policy(policy.policy_id)
    assert after_second.metadata["no_progress_streak"] == 2
    assert after_second.next_due_at == after_first.next_due_at + 1 + 240

    # 连续无进展只封顶不停机:streak 再涨,倍数封在 8×
    scheduler.tick(now=after_second.next_due_at + 1)
    scheduler.tick(now=store.get_progress_policy(policy.policy_id).next_due_at + 1)
    capped = store.get_progress_policy(policy.policy_id)
    assert capped.metadata["no_progress_streak"] == 4
    assert capped.next_due_at == capped.last_report_at + 480  # 60 × 8 封顶
    assert capped.enabled is True  # 退避≠退休

    # 有成功工具调用 → streak 归零、间隔复原
    runtime.tool_success_count = 1
    scheduler.tick(now=capped.next_due_at + 1)
    recovered = store.get_progress_policy(policy.policy_id)
    assert recovered.metadata["no_progress_streak"] == 0
    assert recovered.next_due_at == recovered.last_report_at + 60


def test_scheduler_runs_one_duplicate_progress_policy_per_target(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "重复提醒只跑一次", 'now': 11.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 13.0})

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert [item["reason"] for item in scheduler.last_progress_policy_suppressed] == ["duplicate_policy"]


def test_urgent_wake_uses_full_background_tool_profile(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "紧急事件", 'now': 10.0})

    runtime.run_once({
        "thread_id": thread.thread_id,
        "task_id": "task-1",
        "reason": "urgent_wake_signal",
        "wake_signal": {
            "wake_signal_id": "wake-1",
            "thread_id": thread.thread_id,
            "urgency": "urgent",
            "summary": "需要主代理马上处理。",
        },
        "now": 20.0,
    })
    prompt = backend.prompts[0]

    assert "create_subagents" in prompt
    assert "raise_collaboration" in prompt
    assert "submit_collaboration_result" in prompt


def test_background_runtime_uses_configured_allowed_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            background_main_agent_allowed_tools=["inspect_agent_tree", "send_guidance"],
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "长期后台任务", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "只读看树并提醒", 'now': 12.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 13.0})

    scheduler.tick(now=73.0)
    prompt = backend.prompts[0]

    assert "inspect_agent_tree" in prompt
    assert "send_guidance" in prompt
    assert "dispatch_subagents" not in prompt
    assert "create_subagents" not in prompt


def test_background_runtime_applies_owner_disabled_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.owner_policy = type("OwnerPolicy", (), {"disabled_tools": ("create_subagents", "raise_collaboration")})()
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "紧急事件", 'now': 10.0})

    runtime.run_once({
        "thread_id": thread.thread_id,
        "task_id": "task-1",
        "reason": "urgent_wake_signal",
        "wake_signal": {"urgency": "urgent", "summary": "需要处理。"},
        "now": 20.0,
    })
    prompt = backend.prompts[0]

    assert "create_subagents: 创建" not in prompt
    assert "raise_collaboration: 发起" not in prompt
    assert "removed_tools" in prompt


def test_background_runtime_applies_wake_policy_snapshot(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "策略快照", 'now': 10.0})

    runtime.run_once({
        "thread_id": thread.thread_id,
        "task_id": "task-1",
        "reason": "urgent_wake_signal",
        "wake_signal": {
            "urgency": "urgent",
            "summary": "只允许观察。",
            "policy_snapshot": {"allowed_tools": ["inspect_agent_tree"]},
        },
        "now": 20.0,
    })
    prompt = backend.prompts[0]

    assert "inspect_agent_tree" in prompt
    assert "dispatch_subagents: 只有需要推进" not in prompt
    assert "create_subagents: 创建" not in prompt


def test_background_context_budget_truncates_large_messages(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "长上下文后台任务", 'now': 10.0})
    long_message = "A" * 12000
    store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': long_message,
        'channel': "internal",
        'metadata': {"gateway_request_id": "task-1"},
        'now': 11.0,
    })
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "检查长上下文裁剪", 'now': 12.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'now': 13.0})

    reports = scheduler.tick(now=73.0)
    prompt = backend.prompts[0]

    assert len(reports) == 1
    assert "A" * 2000 not in prompt
    assert "truncated" in prompt


def test_scheduler_recovers_due_policy_after_process_restart(tmp_path) -> None:
    first_store = ConversationStore(tmp_path / "conversations")
    thread = first_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "feishu", 'channel_conversation_id': "chat-1", 'channel_user_id': "open-id-1", 'now': 100.0})
    first_store.append_message({
        'thread_id': thread.thread_id,
        'role': "user",
        'content': "一小时后继续检查。",
        'channel': "feishu",
        'metadata': {"gateway_request_id": "task-1"},
        'now': 101.0,
    })
    first_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "重启后继续", 'now': 102.0})
    first_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 3600, 'route_channel': "feishu", 'route_target': "chat-1", 'now': 103.0})

    restarted_agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    restarted_agent.backend = backend
    restarted_store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=restarted_agent,
        store=restarted_store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': restarted_store})

    reports = scheduler.tick(now=3703.0)

    assert len(reports) == 1
    assert "一小时后继续检查" in backend.prompts[0]
    assert channels.adapter("feishu").sent_messages[0].target == "chat-1"


def test_scheduler_skips_thread_with_active_background_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "避免重复唤醒", 'now': 2.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'now': 3.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "already_running", 'lease_seconds': 300, 'now': 63.0})

    reports = scheduler.tick(now=64.0)

    assert claim is not None
    assert reports == []
    assert backend.prompts == []


def test_scheduler_renews_background_claim_while_runtime_is_still_running(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _SlowBackend(sleep_seconds=1.2)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'claim_ttl_seconds': 1, 'claim_heartbeat_interval_seconds': 0.2})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长后台运行要续租", 'now': 2.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 1, 'now': 3.0})

    reports = scheduler.tick(now=4.0)

    assert reports[0].response == "后台主代理慢速检查完成。"
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    claim = claim_path.read_text(encoding="utf-8")
    assert '"status": "finished"' in claim
    assert '"heartbeat_at": 4.0' not in claim


def test_scheduler_default_heartbeat_interval_stays_below_small_ttl(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'claim_ttl_seconds': 9})

    assert scheduler.claim_heartbeat_interval_seconds == 3.0


def test_background_claim_immediately_takes_over_dead_same_host_owner(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 900, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {
        "host_id": process_host_id(),
        "pid": 999_999_999,
        "start_time": 1,
    }
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    second = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
    )

    assert second is not None
    assert second["claim_id"] != first["claim_id"]
    assert second["acquisition"]["reason"] == "owner_process_stale"
    assert second["previous_claim"]["expired"] is False


def test_background_claim_legacy_owner_waits_for_ttl_instead_of_guessing(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload.pop("owner_process", None)
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
    ) is None


def test_background_claim_different_process_domain_waits_for_ttl(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {"host_id": "another-process-domain", "pid": 999_999_999}
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
    ) is None


def test_scheduler_marks_background_claim_failed_when_runtime_raises(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _FailingBackend()
    agent._current_tool = "web_fetch"
    agent._last_progress_summary = "正在核对来源"
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'claim_ttl_seconds': 30})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "失败时留下可接手事实", 'now': 2.0})
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 1, 'now': 3.0})

    try:
        scheduler.tick(now=4.0)
    except RuntimeError:
        pass

    claim = json.loads((store.background_claims_dir / f"{thread.thread_id}.json").read_text(encoding="utf-8"))
    assert claim["status"] == "failed"
    assert claim["task_id"] == "task-1"
    assert claim["last_error"]["type"] == "RuntimeError"
    assert "backend boom" in claim["last_error"]["message"]
    assert claim["takeover"]["allowed"] is True
    assert claim["takeover"]["reason"] == "runtime_failed"
    assert claim["phase"] == "failed"
    assert claim["last_runtime_facts"]["current_tool"] == "web_fetch"
    assert claim["last_runtime_facts"]["last_progress_summary"] == "正在核对来源"
    assert "tree_status_buckets" in claim["last_runtime_facts"]


def test_background_prompt_includes_recovery_snapshot_for_previous_failed_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "接手时先对账", 'now': 2.0})
    failed = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "previous_run", 'lease_seconds': 10, 'now': 3.0})
    assert failed is not None
    store.finish_background_run({
        'thread_id': thread.thread_id,
        'claim_id': failed["claim_id"],
        'status': "failed",
        'task_id': "task-1",
        'error': {"type": "RuntimeError", "message": "previous run crashed"},
        'now': 4.0,
    })
    store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 1, 'now': 5.0})

    scheduler.tick(now=7.0)
    prompt = backend.prompts[0]

    assert "Recovery Snapshot" in prompt
    assert '"previous_claim_status": "failed"' in prompt
    assert '"takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。"' in prompt


def test_background_claim_unknown_finish_status_is_explicit_protocol_error(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "unknown_status", 'lease_seconds': 10, 'now': 2.0})
    assert claim is not None

    finished = store.finish_background_run({
        'thread_id': thread.thread_id,
        'claim_id': claim["claim_id"],
        'status': "succeeded",
        'now': 3.0,
    })

    assert finished is not None
    assert finished["status"] == "invalid_status"
    assert finished["takeover"] == {"allowed": True, "reason": "runtime_invalid_status"}
    assert finished["last_error"]["type"] == "InvalidBackgroundClaimStatus"


def test_cancelled_background_claim_is_not_recovery_takeover_candidate(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "user_work", 'lease_seconds': 10, 'now': 2.0})
    assert claim is not None

    finished = store.finish_background_run({
        'thread_id': thread.thread_id,
        'claim_id': claim["claim_id"],
        'status': "cancelled",
        'now': 3.0,
    })

    assert finished is not None
    assert finished["takeover"] == {"allowed": False, "reason": "user_interrupted"}


# ── 后台 claim 心跳:线程缺失不得裸崩 daemon 线程(修多 owner ticking 下 KeyError 崩心跳) ──

def test_renew_background_run_claim_present_thread_still_renews(tmp_path) -> None:
    """行为保持:线程在时续租照常成功、写入新的 heartbeat_at/expires_at。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "u1", 'channel': "internal", 'channel_conversation_id': "c1", 'channel_user_id': "u1", 'now': 1.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "wake_signal", 'lease_seconds': 30, 'now': 2.0})
    assert claim is not None

    renewed = store.renew_background_run_claim({'thread_id': thread.thread_id, 'claim_id': claim["claim_id"], 'lease_seconds': 30, 'now': 5.0})

    assert renewed is not None
    assert renewed["heartbeat_at"] == 5.0
    assert renewed["expires_at"] == 35.0


def test_renew_background_run_claim_missing_thread_returns_none_not_keyerror(tmp_path) -> None:
    """根因修:线程文件在长跑中消失(边缘/竞态)时,续租返回 None 让心跳优雅停机,绝不抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "u1", 'channel': "internal", 'channel_conversation_id': "c1", 'channel_user_id': "u1", 'now': 1.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "wake_signal", 'lease_seconds': 30, 'now': 2.0})
    assert claim is not None
    # 模拟真机现象:claim 成功后线程文件不再可读(store 根竞态/外部清理/长跑中消失)。
    (store.threads_dir / f"{thread.thread_id}.json").unlink()

    renewed = store.renew_background_run_claim({'thread_id': thread.thread_id, 'claim_id': claim["claim_id"], 'lease_seconds': 30, 'now': 5.0})

    assert renewed is None  # 修前:此处抛 KeyError('unknown conversation thread') 崩心跳线程


def test_finish_background_run_missing_thread_finalizes_claim_without_crash(tmp_path) -> None:
    """收尾在 _run_with_heartbeat 的 finally 跑:线程缺失也要能释放已存在的 claim 租约,绝不二次抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "u1", 'channel': "internal", 'channel_conversation_id': "c1", 'channel_user_id': "u1", 'now': 1.0})
    claim = store.claim_background_run({'thread_id': thread.thread_id, 'reason': "wake_signal", 'lease_seconds': 30, 'now': 2.0})
    assert claim is not None
    (store.threads_dir / f"{thread.thread_id}.json").unlink()

    finished = store.finish_background_run({'thread_id': thread.thread_id, 'claim_id': claim["claim_id"], 'status': "finished", 'now': 5.0})

    assert finished is not None
    assert finished["status"] == "finished"


def test_finish_background_run_no_claim_file_returns_none_without_crash(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    assert store.finish_background_run({'thread_id': "thread-never", 'claim_id': "x", 'status': "finished", 'now': 1.0}) is None


def test_background_claim_heartbeat_stops_gracefully_when_renew_raises() -> None:
    """防御纵深:renew 抛任何异常时,daemon 心跳线程记账后优雅停机,不把未捕获异常抛出杀线程。"""
    import threading as _threading

    from agent_py_agent.agent.conversation.run_claim import ConversationRunClaimHeartbeat

    class _RaisingStore:
        def renew_background_run_claim(self, request: dict):
            raise KeyError("unknown conversation thread: thread-boom")

    heartbeat = ConversationRunClaimHeartbeat({'store': _RaisingStore(), 'thread_id': "thread-boom", 'claim_id': "c1", 'lease_seconds': 1, 'interval_seconds': 0.05})
    uncaught: list[type] = []
    previous_hook = _threading.excepthook
    _threading.excepthook = lambda args: uncaught.append(args.exc_type)
    try:
        heartbeat.start()
        time.sleep(0.3)
        heartbeat.join(timeout=2.0)
    finally:
        _threading.excepthook = previous_hook

    assert not heartbeat.is_alive()  # 线程已优雅退出
    assert uncaught == []  # 没有未捕获异常杀线程(修前:KeyError 裸崩 "Exception in thread")
