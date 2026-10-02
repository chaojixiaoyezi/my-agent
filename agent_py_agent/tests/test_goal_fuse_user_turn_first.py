"""C5/O4（2026-10-02）：持续目标的空片熔断与排队中的用户消息——用户消息先处理。

真实链路（隔离 Gateway + TUI + 脚本化假模型，代码 f9ca83242）：第 3 个空片还在跑时用户发来消息，消息约 0.8 秒后转成下一轮排队，
第 3 片结束记账时熔断暂停，用户回合随后才写入会话、只把计数清零，目标仍暂停、要用户 /goal resume。规则：
1. 携带用户消息的回合（网关前台回合）从开始排队等车道到退出车道，都在进程内登记为“用户回合在场”（run_claim）；
2. 空片结束记账时本会话有用户回合在场：这一片不计入、计数保持原值，续跑链照常接下一片；有进展的片照常清零；
3. 清零仍由用户消息写入会话后的 D4 重置完成（写不进去就不清，见 test_gateway_goal_fuse_reset.py）。
4. 判定时点（sol2 竞态，2026-10-02）：熔断读“在场”与落账在同一把车道闸里完成，用户回合登记也要过这把闸——
   要么登记在判定之前（空片不计入），要么等落账做完再登记（暂停已先发生，D4 只清计数、不自动恢复）。
Gateway 端到端的顺序见 test_gateway_goal_fuse_reset.py::test_user_message_queued_during_the_third_idle_slice_is_handled_first
与 ::test_user_message_arriving_during_the_fuse_commit_waits_for_the_decision。
"""
from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_goal import (
    GoalContinuationDependencies,
    continue_goal_after_report,
)
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    WAKE_SNAPSHOT_KEY,
    goal_progress_snapshot,
)
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport, WakeSignal
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunLaneRequest,
    conversation_run_lane,
    user_input_turn_gate,
    user_input_turn_on_lane,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


@pytest.fixture
def lane_store(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[]),
                        tmp_path / "ws")
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"canonical_user_id": "test-user", "channel": "internal", "channel_conversation_id": "c5-lane"})
    return store, thread.thread_id, agent


def _lane(store, thread_id, *, user_input: bool, interrupt=lambda: False) -> ConversationRunLaneRequest:
    return ConversationRunLaneRequest(
        store=store, thread_id=thread_id, claim_task_id="gateway:req-c5", reason="gateway_foreground",
        lease_seconds=60, heartbeat_interval_seconds=30.0, interrupt_check=interrupt, carries_user_input=user_input)


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.01)


def test_user_turn_is_on_the_lane_from_queueing_until_it_leaves(lane_store):
    store, thread_id, _agent = lane_store
    holder = store.claims.acquire({"thread_id": thread_id, "task_id": "bg-slice", "reason": "background",
                                   "lease_seconds": 60})
    inside, leave = threading.Event(), threading.Event()

    def user_turn() -> None:
        with conversation_run_lane(_lane(store, thread_id, user_input=True)):
            inside.set()
            leave.wait(10)

    worker = threading.Thread(target=user_turn, daemon=True)
    worker.start()
    # 后台片仍持有车道：用户回合在排队，已登记在场。
    _wait_until(lambda: user_input_turn_on_lane(store, thread_id))
    assert not inside.is_set()
    store.claims.finish({"thread_id": thread_id, "claim_id": holder["claim_id"], "task_id": "bg-slice",
                         "status": "finished"})
    # 拿到车道后仍在场：后台片释放车道之后才记账，这段空档也要算用户回合在场。
    _wait_until(inside.is_set)
    assert user_input_turn_on_lane(store, thread_id)
    leave.set()
    worker.join(10)
    assert not user_input_turn_on_lane(store, thread_id)


def test_other_lanes_and_abandoned_waits_do_not_stay_registered(lane_store):
    from agent_py_agent.agent.gateway_parts.control_service import _manual_compact_lane

    store, thread_id, agent = lane_store
    with _manual_compact_lane(agent, store, thread_id):
        # 产品里的手动 Compact 车道不携带用户消息（用缺省值），不登记。
        assert not user_input_turn_on_lane(store, thread_id)
    stop = threading.Event()
    seen = []

    def interrupted() -> bool:
        seen.append(user_input_turn_on_lane(store, thread_id))
        return stop.is_set()

    stop.set()
    with pytest.raises(InterruptedError), conversation_run_lane(_lane(store, thread_id, user_input=True,
                                                                       interrupt=interrupted)):
        pass
    # 排队期间登记在场，中断放弃排队后撤销。
    assert seen == [True]
    assert not user_input_turn_on_lane(store, thread_id)


def _dependencies(recorded: list, raised: list, *, user_on_lane: bool) -> GoalContinuationDependencies:
    goal = SimpleNamespace(goal_id="goal-1", task_id="task-1", thread_id="thread-1", status="active", revision=1)
    return GoalContinuationDependencies(
        goals=SimpleNamespace(load=lambda _thread, goal_id="": goal,
                              record_continuation_fuse=lambda request: recorded.append(request) or (goal, False)),
        tasks=SimpleNamespace(update_status=lambda _request: None),
        goal_clock=SimpleNamespace(),
        task_status=lambda _thread, _task: "active",
        subagent_phase=lambda _task: ("idle", ""),
        raise_wake=lambda *args, **kwargs: raised.append(args),
        task_registry=lambda: None,
        user_turn_gate=lambda thread_id: nullcontext(user_on_lane and thread_id == "thread-1"),
    )


def _report(tool_calls: int) -> BackgroundMainAgentReport:
    return BackgroundMainAgentReport(thread_id="thread-1", task_id="task-1", reason="thread_goal_continue",
                                     response="", route_channel="", route_target="", created_at=1.0,
                                     goal_continuation_allowed=True, tool_call_count=tool_calls)


def test_idle_slice_is_not_counted_while_a_user_turn_is_on_the_lane():
    recorded, raised = [], []
    dependencies = _dependencies(recorded, raised, user_on_lane=True)
    goal = dependencies.goals.load("thread-1")
    signal = WakeSignal(wake_signal_id="wake-3", thread_id="thread-1", root_task_id="task-1",
                        metadata={"goal_id": "goal-1", WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, "active")})

    continue_goal_after_report(dependencies, signal, report=_report(0), now=2.0)
    # 用户消息先处理：空片不记账（计数保持原值），续跑链照常接下一片。
    assert recorded == [] and len(raised) == 1

    continue_goal_after_report(dependencies, signal, report=_report(1), now=3.0)
    # 有进展的片照常记账清零，不因用户回合在场而丢掉。
    assert [request["progressed"] for request in recorded] == [True]

    idle = replace(dependencies, user_turn_gate=lambda _thread_id: nullcontext(False))
    continue_goal_after_report(idle, signal, report=_report(0), now=4.0)
    assert [request["progressed"] for request in recorded] == [True, False]


# ---------------------------------------------------------------- 判定时点：真实车道闸


def _gated_dependencies(store, thread_id: str, commit) -> tuple[GoalContinuationDependencies, WakeSignal]:
    goal = SimpleNamespace(goal_id="goal-1", task_id="task-1", thread_id=thread_id, status="active", revision=1)
    dependencies = GoalContinuationDependencies(
        goals=SimpleNamespace(load=lambda _thread, goal_id="": goal, record_continuation_fuse=commit),
        tasks=SimpleNamespace(update_status=lambda _request: None),
        goal_clock=SimpleNamespace(),
        task_status=lambda _thread, _task: "active",
        subagent_phase=lambda _task: ("idle", ""),
        raise_wake=lambda *args, **kwargs: None,
        task_registry=lambda: None,
        queue_no_progress_notice=lambda _goal: True,
        user_turn_gate=partial(user_input_turn_gate, store),
    )
    signal = WakeSignal(wake_signal_id="wake-3", thread_id=thread_id, root_task_id="task-1",
                        metadata={"goal_id": "goal-1", WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, "active")})
    return dependencies, signal


def test_user_turn_registration_waits_while_the_fuse_decides_and_commits(lane_store):
    """sol2 竞态：第 3 个空片判定“不在场”后、暂停落盘前到达的用户回合，必须等落账做完才登记，不能插在中间。"""
    store, thread_id, _agent = lane_store
    committing, release, recorded = threading.Event(), threading.Event(), []

    def slow_commit(request: dict) -> tuple[object, bool]:
        recorded.append(request)
        committing.set()
        assert release.wait(10)
        return SimpleNamespace(goal_id="goal-1"), True  # 熔断暂停

    dependencies, signal = _gated_dependencies(store, thread_id, slow_commit)
    fuse = threading.Thread(target=continue_goal_after_report, args=(dependencies, signal),
                            kwargs={"report": _report(0), "now": 2.0}, daemon=True)
    fuse.start()
    assert committing.wait(10)  # 已判定“不在场”，正在落账
    entered = threading.Event()

    def user_turn() -> None:
        with conversation_run_lane(_lane(store, thread_id, user_input=True)):
            entered.set()

    user = threading.Thread(target=user_turn, daemon=True)
    user.start()
    user.join(0.3)
    assert not user_input_turn_on_lane(store, thread_id) and not entered.is_set()  # 登记被车道闸挡在落账之后
    release.set()
    fuse.join(10)
    user.join(10)
    assert entered.is_set() and not user.is_alive() and not fuse.is_alive()
    assert [request["progressed"] for request in recorded] == [False]
    assert not user_input_turn_on_lane(store, thread_id)


def test_user_turn_registered_before_the_decision_skips_the_idle_slice(lane_store):
    store, thread_id, _agent = lane_store
    recorded, inside, leave = [], threading.Event(), threading.Event()

    def user_turn() -> None:
        with conversation_run_lane(_lane(store, thread_id, user_input=True)):
            inside.set()
            leave.wait(10)

    user = threading.Thread(target=user_turn, daemon=True)
    user.start()
    assert inside.wait(10)
    dependencies, signal = _gated_dependencies(store, thread_id, lambda request: recorded.append(request) or (None, False))
    continue_goal_after_report(dependencies, signal, report=_report(0), now=2.0)
    assert recorded == []  # 用户回合先登记：真实车道闸给出“在场”，空片不计入
    leave.set()
    user.join(10)
    continue_goal_after_report(dependencies, signal, report=_report(0), now=3.0)
    assert [request["progressed"] for request in recorded] == [False]  # 用户回合离开后照常记账
