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


def test_scheduler_retires_stale_missed_progress_policy_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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


def test_scheduler_retires_terminal_task_progress_policy_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "终态任务退休watch", 'now': 11.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    store.update_task_status({'task_id': "task-1", 'status': "DONE", 'now': 70.0})

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "terminal_task_link"
    # 被观察任务已终态(DONE),watch 策略应退休(enabled=False),不再每个间隔唤醒后台主代理发
    # LLM 进度汇报(churn 根因)。这里 now=100 未到 stale 窗口,确保抑制原因是终态而非陈旧。
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


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
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
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
    from agent_py_agent.agent.conversation.runtime import _BackgroundClaimHeartbeat

    class _RaisingStore:
        def renew_background_run_claim(self, request: dict):
            raise KeyError("unknown conversation thread: thread-boom")

    heartbeat = _BackgroundClaimHeartbeat({'store': _RaisingStore(), 'thread_id': "thread-boom", 'claim_id': "c1", 'lease_seconds': 1, 'interval_seconds': 0.05})
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
