# LLM: C11 体验修复的回归测试：Goal 空转片不再主动推外部通道，熔断提示对外发线程只主动推一次。
#   全部用隔离临时 store + 假模型 + 假投递服务，不启动 Gateway、不联网、不读真实会话。
# 模块用途: 验证空片不外发/有进展外发/熔断提示去重/纯 TUI 不变四条合同。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    FUSE_METADATA_KEY,
    NO_PROGRESS_REASON_CODE,
    push_goal_no_progress_notice,
)
from agent_py_agent.agent.conversation.goal_runtime import raise_goal_continuation_wake
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# LLM: 假后端只返回固定正文、从不调用工具，因此每一片都是“无进展”空片；有进展场景用工具后端。
# 类用途: 为 Goal 续跑空片测试提供无工具调用的模型替身。
class _IdleBackend:
    name = "idle"

    def probe_tool_capability(self):
        return None

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        return ModelResponse(text="在等你", backend=self.name)


# LLM: 第一次返回 list_files 工具调用（真实执行一次工具算进展），之后回到空片。
# 类用途: 为“有进展片照常外发、之后空片不外发”的混合场景提供模型替身。
class _ProgressThenIdleBackend:
    name = "progress-then-idle"

    def __init__(self) -> None:
        self.calls = 0

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://bg-test",
            model="",
            stream=False,
            native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[
                    {
                        "id": "call-goal-list-1",
                        "name": "list_files",
                        "input": {"path": "."},
                    }
                ],
            )
        return ModelResponse(text="在等你", backend=self.name)


# LLM: 线程按 channel 建绑定；feishu 是唯一内置主动外呼通道，internal 是本地转录通道。
# 函数用途: 按通道构造一个带 goal/task 的隔离后台线程环境。
def _goal_env(tmp_path, channel: str):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _IdleBackend()
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": channel,
            "channel_conversation_id": "oc_chat1",
            "channel_user_id": "ou_1",
            "now": 10.0,
        }
    )
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "持续推进同一件工作", "now": 11.0}
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "now": 12.0,
        }
    )
    raise_goal_continuation_wake(store, goal, channel=channel, conversation_id="oc_chat1", now=13.0)
    return agent, store, runtime, scheduler, thread, goal


# LLM: 只取会话历史里正文非空的行（原生空正文事实行不算用户可见回复）。
# 函数用途: 断言空片正文确实落进了权威转录（TUI 可看）。
def _public_contents(store, thread_id):
    return [
        row.content
        for row in store.messages.recent(thread_id)
        if row.content and row.metadata.get("assistant_part_id") != "native"
    ]


def test_feishu_idle_slices_are_not_pushed_and_fuse_pushes_exactly_one_notice(tmp_path) -> None:
    agent, store, runtime, scheduler, thread, goal = _goal_env(tmp_path, channel="feishu")

    reports = [scheduler.tick(now=float(14 + index)) for index in range(3)]

    assert all(len(batch) == 1 and batch[0].tool_call_count == 0 for batch in reports)
    sent = runtime.channels.adapter("feishu").sent_messages
    # 3 个空片正文都不主动推给飞书；只有第 3 片熔断后恰好 1 条提示。
    assert len(sent) == 1
    assert "在等你" not in sent[0].content
    assert "自动续跑已暂停" in sent[0].content
    updated = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert updated is not None and updated.status == "paused"
    assert updated.metadata[FUSE_METADATA_KEY]["reason_code"] == NO_PROGRESS_REASON_CODE
    # 提示已主动送达并取走：下一条回复不会再重复带出。
    assert store.threads.load(thread.thread_id).pending_host_notices == ()
    # 空片正文照常落权威转录（TUI 可见），共 3 条“在等你”；提示也留了一条会话记录。
    contents = _public_contents(store, thread.thread_id)
    assert contents.count("在等你") == 3
    assert any("自动续跑已暂停" in item for item in contents)


def test_progress_slice_is_pushed_but_following_idle_slices_are_not(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _ProgressThenIdleBackend()
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "oc_chat1",
            "channel_user_id": "ou_1",
            "now": 10.0,
        }
    )
    goal = store.goals.create(
        {"thread_id": thread.thread_id, "objective": "持续推进同一件工作", "now": 11.0}
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "now": 12.0,
        }
    )
    raise_goal_continuation_wake(store, goal, channel="feishu", conversation_id="oc_chat1", now=13.0)

    reports = [scheduler.tick(now=float(14 + index)) for index in range(4)]

    assert [batch[0].tool_call_count for batch in reports] == [1, 0, 0, 0]
    sent = runtime.channels.adapter("feishu").sent_messages
    # 第 1 片有进展照常外发；第 2、3 片空片不外发；第 4 片熔断推 1 条提示。
    assert len(sent) == 2
    assert sent[0].content == "在等你"
    assert "自动续跑已暂停" in sent[1].content
    updated = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert updated is not None and updated.status == "paused"
    assert store.threads.load(thread.thread_id).pending_host_notices == ()


def test_tui_thread_without_external_channel_keeps_original_behavior(tmp_path) -> None:
    agent, store, runtime, scheduler, thread, goal = _goal_env(tmp_path, channel="internal")

    reports = [scheduler.tick(now=float(14 + index)) for index in range(3)]

    assert all(len(batch) == 1 and batch[0].tool_call_count == 0 for batch in reports)
    # 纯 TUI 线程：空片正文照旧落本地转录，没有外部通道可推，提示留在待送达队列。
    sent = runtime.channels.adapter("internal").sent_messages
    assert len(sent) == 3
    assert all(item.content == "在等你" for item in sent)
    assert store.threads.load(thread.thread_id).pending_host_notices != ()
    updated = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert updated is not None and updated.status == "paused"


def test_push_notice_queues_without_external_channel_but_pushes_and_takes_with_feishu(tmp_path) -> None:
    from agent_py_agent.agent.conversation.background_routing import BackgroundRouteDependencies

    agent, store, runtime, scheduler, thread, goal = _goal_env(tmp_path, channel="feishu")
    paused_goal, tripped = store.goals.record_continuation_fuse(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "task_id": goal.task_id,
            "wake_signal_id": "push-wake",
            "progressed": False,
            "idle_limit": 1,
        }
    )
    assert tripped and paused_goal is not None
    route_deps = BackgroundRouteDependencies(
        load_thread=lambda tid: store.threads.load(tid),
        owner_paths=lambda: (),
        owner_identity=lambda: ("", ""),
    )

    assert push_goal_no_progress_notice(
        store, paused_goal, channels=runtime.channels, route_deps=route_deps
    )
    assert len(runtime.channels.adapter("feishu").sent_messages) == 1
    assert "自动续跑已暂停" in runtime.channels.adapter("feishu").sent_messages[0].content
    assert store.threads.load(thread.thread_id).pending_host_notices == ()

    # 纯 TUI 线程（internal binding）只排队不推送。
    agent2 = SimpleAgent(AgentConfig(memory_path="memory.jsonl"), tmp_path / "tui")
    store2 = agent2.conversation_store
    thread2 = store2.threads.get_or_create(
        {
            "canonical_user_id": "user-2",
            "channel": "internal",
            "channel_conversation_id": "tui-thread",
            "channel_user_id": "user-2",
        }
    )
    goal2 = store2.goals.create({"thread_id": thread2.thread_id, "objective": "本地目标"})
    store2.tasks.bind(
        {"thread_id": thread2.thread_id, "task_id": goal2.task_id, "goal": goal2.objective, "status": "active"}
    )
    paused2, tripped2 = store2.goals.record_continuation_fuse(
        {
            "thread_id": thread2.thread_id,
            "goal_id": goal2.goal_id,
            "task_id": goal2.task_id,
            "wake_signal_id": "push-wake-2",
            "progressed": False,
            "idle_limit": 1,
        }
    )
    assert tripped2 and paused2 is not None
    channels2 = FakeDeliveryService()
    assert push_goal_no_progress_notice(
        store2, paused2, channels=channels2, route_deps=route_deps
    )
    assert channels2.adapter("internal").sent_messages == []
    assert len(store2.threads.load(thread2.thread_id).pending_host_notices) == 1