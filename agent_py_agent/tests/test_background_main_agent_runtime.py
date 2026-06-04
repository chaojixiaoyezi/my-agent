from __future__ import annotations

import json
import time

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeChannelHub,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="后台主代理已检查任务树，并给出阶段汇报。", backend=self.name)


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


def test_background_runtime_reports_corrupt_thread_before_running_model(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())

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
    channels = FakeChannelHub()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})

    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "长期后台任务", 'now': 10.0})
    store.append_message({'thread_id': thread.thread_id, 'role': "user", 'content': "每小时帮我看一次进展，有问题就调度。", 'channel': "internal", 'now': 11.0})
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


def test_scheduler_records_bad_progress_policy_without_blocking_due_policy(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeChannelHub()
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


def test_urgent_wake_uses_full_background_tool_profile(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    channels = FakeChannelHub()
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': "长上下文后台任务", 'now': 10.0})
    long_message = "A" * 12000
    store.append_message({'thread_id': thread.thread_id, 'role': "user", 'content': long_message, 'channel': "internal", 'now': 11.0})
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
    first_store.append_message({'thread_id': thread.thread_id, 'role': "user", 'content': "一小时后继续检查。", 'channel': "feishu", 'now': 101.0})
    first_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "重启后继续", 'now': 102.0})
    first_store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 3600, 'route_channel': "feishu", 'route_target': "chat-1", 'now': 103.0})

    restarted_agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    restarted_agent.backend = backend
    restarted_store = ConversationStore(tmp_path / "conversations")
    channels = FakeChannelHub()
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())

    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'claim_ttl_seconds': 9})

    assert 0 < scheduler.claim_heartbeat_interval_seconds < scheduler.claim_ttl_seconds


def test_scheduler_marks_background_claim_failed_when_runtime_raises(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _FailingBackend()
    agent._current_tool = "web_fetch"
    agent._last_progress_summary = "正在核对来源"
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
