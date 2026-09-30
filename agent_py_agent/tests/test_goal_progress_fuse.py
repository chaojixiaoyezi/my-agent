from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.conversation.goal_progress_fuse import (
    FUSE_METADATA_KEY,
    NO_PROGRESS_REASON_CODE,
    WAKE_SNAPSHOT_KEY,
    goal_progress_snapshot,
    next_idle_slice_count,
    queue_goal_no_progress_notice,
    reset_goal_progress_fuse,
    slice_has_progress,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# LLM: 用精确的临时 thread/Goal/task 构造真实持久状态，不接触真实会话或 owner 数据。
# 函数用途: 为无进展熔断合同测试创建隔离的 ConversationStore。
def _goal_store(tmp_path):
    agent = SimpleAgent(AgentConfig(memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"canonical_user_id": "test-user", "channel": "internal", "channel_conversation_id": "fuse"}
    )
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "推进工作"})
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active"}
    )
    return store, thread, goal


# LLM: 阈值按实际连续空片数比较，达到上限时只停止本 Goal 自动续跑。
# 函数用途: 验证连续三个无工具且无结构变化的片在第 3 片触发熔断。
def test_empty_slices_trip_at_configured_limit():
    count = 0
    decisions = []
    for _ in range(3):
        count, tripped = next_idle_slice_count(count, progressed=False, limit=3)
        decisions.append(tripped)
    assert decisions == [False, False, True]
    assert count == 3


# LLM: 工具调用和 Goal/任务快照差异都是结构化进展；不读取 response 或消息 content。
# 函数用途: 验证实际进展清零，并能识别无工具调用时的 Goal revision 变化。
def test_tool_or_goal_revision_progress_resets_idle_counter():
    goal = SimpleNamespace(goal_id="goal-1", revision=2, status="active", task_id="task-1")
    signal = SimpleNamespace(
        metadata={WAKE_SNAPSHOT_KEY: {**goal_progress_snapshot(goal, "active"), "goal_revision": 1}}
    )
    empty_report = SimpleNamespace(tool_call_count=0, material_progress_count=0)
    assert slice_has_progress(signal, empty_report, goal, "active") is True
    assert next_idle_slice_count(2, progressed=True, limit=3) == (0, False)

    tool_report = SimpleNamespace(tool_call_count=1, material_progress_count=0)
    signal.metadata[WAKE_SNAPSHOT_KEY] = goal_progress_snapshot(goal, "active")
    assert slice_has_progress(signal, tool_report, goal, "active") is True
    assert slice_has_progress(signal, empty_report, goal, "completed") is True


# LLM: 0 是明确不限，不积攒隐藏次数，之后切回有限值也不会继承陈旧空片数。
# 函数用途: 验证配置关闭熔断时不会达到任何暂停阈值。
def test_zero_limit_is_unlimited():
    count = 0
    for _ in range(20):
        count, tripped = next_idle_slice_count(count, progressed=False, limit=0)
        assert not tripped
    assert count == 0


# LLM: 用户输入可清空计数但不擅自恢复暂停状态；explicit resume 才清原因码。
# 函数用途: 验证已有 GoalStore 能持久重置 fuse 并保留其它目标状态。
def test_reset_clears_fuse_count_and_reason(tmp_path):
    store, thread, goal = _goal_store(tmp_path)
    for index in range(2):
        store.goals.record_continuation_fuse(
            {
                "thread_id": thread.thread_id,
                "goal_id": goal.goal_id,
                "task_id": goal.task_id,
                "wake_signal_id": f"wake-{index}",
                "progressed": False,
                "idle_limit": 3,
            }
        )
    current = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert current.metadata[FUSE_METADATA_KEY]["idle_slices"] == 2

    assert reset_goal_progress_fuse(store, thread.thread_id, clear_reason=True)

    current = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert current.status == "active"
    assert current.metadata[FUSE_METADATA_KEY]["idle_slices"] == 0
    assert current.metadata[FUSE_METADATA_KEY]["reason_code"] == ""


# LLM: 用 GoalStore 每次独立原子记账，验证进展后下一片重新从 1 起算，而非只测纯计数器。
# 函数用途: 覆盖连续空片中夹入结构化进展时持久计数归零。
def test_structured_progress_resets_persisted_idle_streak(tmp_path):
    store, thread, goal = _goal_store(tmp_path)
    for index, progressed, expected in (
        (0, False, 1),
        (1, True, 0),
        (2, False, 1),
    ):
        store.goals.record_continuation_fuse(
            {
                "thread_id": thread.thread_id,
                "goal_id": goal.goal_id,
                "task_id": goal.task_id,
                "wake_signal_id": f"progress-wake-{index}",
                "progressed": progressed,
                "idle_limit": 3,
            }
        )
        current = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
        assert current.metadata[FUSE_METADATA_KEY]["idle_slices"] == expected


# LLM: 用户消息只通过真实 ChannelMessageRuntime ingress 重置计数；测试不启后台模型，也不读取消息正文。
# 函数用途: 验证新用户消息到达后，当前 active Goal 从新的空片计数周期开始。
def test_channel_user_message_resets_goal_idle_count(tmp_path):
    from agent_py_agent.agent.conversation.runtime import (
        ChannelMessageRuntime,
        _agent_owner_home,
        _agent_owner_id,
    )

    agent = SimpleAgent(AgentConfig(memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    channel_values = {
        "canonical_user_id": "test-user",
        "channel": "internal",
        "channel_conversation_id": "channel-reset",
        "channel_user_id": "test-user",
        "owner_id": _agent_owner_id(agent),
        "owner_home": _agent_owner_home(agent),
    }
    thread = store.threads.get_or_create(channel_values)
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "新消息后重计"})
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active"}
    )
    for index in range(2):
        store.goals.record_continuation_fuse(
            {
                "thread_id": thread.thread_id,
                "goal_id": goal.goal_id,
                "task_id": goal.task_id,
                "wake_signal_id": f"prior-idle-{index}",
                "progressed": False,
                "idle_limit": 3,
            }
        )

    receive = ChannelMessageRuntime(
        runtime=SimpleNamespace(agent=agent, run_once=lambda _request: None), store=store
    )
    receive.receive({**channel_values, "content": "新的用户输入", "run_background": False})

    current = store.goals.load(thread.thread_id, goal_id=goal.goal_id)
    assert current.status == "active"
    assert current.metadata[FUSE_METADATA_KEY]["idle_slices"] == 0


# LLM: 宿主提示复用 source+code 去重；多次收到同一熔断结果仍只保留一条待送达通知。
# 函数用途: 验证 notice 幂等和固定 reason code 可供 TUI/IM 展示。
def test_no_progress_notice_is_deduplicated_by_existing_host_notice_queue(tmp_path):
    store, thread, goal = _goal_store(tmp_path)
    paused_goal, tripped = store.goals.record_continuation_fuse(
        {
            "thread_id": thread.thread_id,
            "goal_id": goal.goal_id,
            "task_id": goal.task_id,
            "wake_signal_id": "idle-wake-notice",
            "progressed": False,
            "idle_limit": 1,
        }
    )
    assert tripped and paused_goal.metadata[FUSE_METADATA_KEY]["reason_code"] == NO_PROGRESS_REASON_CODE

    assert queue_goal_no_progress_notice(store, paused_goal)
    assert queue_goal_no_progress_notice(store, paused_goal)

    current_thread = store.threads.load(thread.thread_id)
    assert len(current_thread.pending_host_notices) == 1
    assert current_thread.pending_host_notices[0]["code"] == NO_PROGRESS_REASON_CODE
    assert current_thread.pending_host_notices[0]["details"]["reason_code"] == NO_PROGRESS_REASON_CODE


# 1 号审查建议：子代理还在跑时，父级这一片不续跑，也不能计成空片（否则父级等子代理期间会被误暂停）。
def test_slices_while_subagents_are_active_neither_count_nor_continue() -> None:
    from dataclasses import replace

    from agent_py_agent.agent.conversation.background_goal import (
        GoalContinuationDependencies,
        continue_goal_after_report,
    )
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport, WakeSignal

    goal = SimpleNamespace(goal_id="goal-1", task_id="task-1", thread_id="thread-1", status="active")
    recorded, raised = [], []
    dependencies = GoalContinuationDependencies(
        goals=SimpleNamespace(load=lambda _thread, goal_id="": goal,
                              record_continuation_fuse=lambda request: recorded.append(request) or (goal, False)),
        tasks=SimpleNamespace(update_status=lambda _request: None),
        goal_clock=SimpleNamespace(),
        task_status=lambda _thread, _task: "active",
        subagent_phase=lambda _task: ("subagents_active", ""),
        raise_wake=lambda *args, **kwargs: raised.append(args),
        task_registry=lambda: None,
    )
    signal = WakeSignal(wake_signal_id="wake-1", thread_id="thread-1", root_task_id="task-1",
                        metadata={"goal_id": "goal-1"})
    report = BackgroundMainAgentReport(thread_id="thread-1", task_id="task-1", reason="thread_goal_continue",
                                       response="", route_channel="", route_target="", created_at=1.0,
                                       goal_continuation_allowed=True)
    continue_goal_after_report(dependencies, signal, report=report, now=2.0)
    assert recorded == [] and raised == []
    # 子代理结束后，同样的空片照常计数并续跑。
    idle = replace(dependencies, subagent_phase=lambda _task: ("idle", ""))
    continue_goal_after_report(idle, signal, report=report, now=3.0)
    assert len(recorded) == 1 and len(raised) == 1
