"""派活回合不能空转：不属于本回合的投递，既不能认领，也不能算作待处理输入。

真实 gateway 上的现象（2026-09-28 dsh-ae 复验 a646a4885）：
- 会话处在派活回合里时，只要邮箱里挂着**别的任务**的完成回报（回报 metadata 带
  `session_task_id`），就会无限重来：A 的派活回合 3.2 秒里调了 88 次模型，只能停 gateway 才止住。
- 根因：`claim_for_turn` 因为 `owning_task_id` 不一致拒绝认领这条回报，但
  `available_for_turn` / `has_pending_request_guidance` **不看任务归属**，照样判定"有新输入"，
  于是每次最终回复都被作废重来，没有上限。

这里证明两件事：
1. "能不能认领"和"算不算待处理输入"是同一个判定（一个概念一个权威位置）；
2. 即使判据出问题，同一回合的作废次数也有硬上限，不会无限重来。

只走真实入口：真实 `ConversationStore` + 真实 `available_for_turn` / `has_pending_request_guidance`。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._tool_loop_service import (
    PENDING_TURN_INPUT_INVALIDATION_COUNT,
    _pending_turn_input_invalidation_exhausted,
)
from agent_py_agent.agent.agent_core.runtime.guidance import has_pending_request_guidance
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_messaging import SESSION_TASK_ORIGIN_KIND
from agent_py_agent.agent.conversation.store import ConversationStore

_SENDER = "thread-a"
_TARGET = "thread-b"
_MY_TASK = "stask-mine"
_OTHER_TASK = "stask-other"


def _store(tmp_path) -> ConversationStore:
    store = ConversationStore(tmp_path / "conv")
    store.threads.write(
        ConversationThread(
            thread_id=_TARGET,
            canonical_user_id="local-agent",
            owner_id="main",
            owner_home="",
            title="目标会话",
        )
    )
    return store


def _params(store: ConversationStore, *, owning_task_id: str = _MY_TASK) -> SimpleNamespace:
    attrs = {"conversation_thread_id": _TARGET}
    if owning_task_id:
        attrs[CONVERSATION_SESSION_TASK_ID_ATTR] = owning_task_id
    return SimpleNamespace(
        request_id=_MY_TASK,
        task_id=_MY_TASK,
        run_id="attempt-1",
        attempt_id="attempt-1",
        context_scope="",
        task_attributes=attrs,
        live_archive_state={},
        tool_context=[],
        runtime_injections=[],
    )


def _agent(store: ConversationStore) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_store=store,
        config=CapabilityConfig(),
        _capability_config_runtime_snapshot=type("_S", (), {"config": CapabilityConfig()})(),
    )


def _queue_report(store: ConversationStore, task_id: str) -> str:
    """另一个任务的完成回报（真实回报条目的 metadata 形状）。"""
    entry = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET,
            "message": f"任务 {task_id} 已完成。",
            "sender": _SENDER,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": _SENDER,
                "session_task_id": task_id,
            },
        },
        dedupe_key=f"session_task_report:{task_id}",
    )
    return entry.guidance_id


def test_other_task_report_is_not_claimable(tmp_path) -> None:
    """别的任务的回报不属于本回合：不能认领。"""
    store = _store(tmp_path)
    _queue_report(store, _OTHER_TASK)
    entry = store.guidance.pending("thread", _TARGET)[0]

    assert store.guidance.claim_for_turn(
        entry, expected_turn_id=_MY_TASK, attempt_id="attempt-1", owning_task_id=_MY_TASK
    ) is False


def test_other_task_report_is_not_pending_input(tmp_path) -> None:
    """同一个判定：它也不能被算成"有待处理输入"——这正是无限空转的根因。"""
    store = _store(tmp_path)
    _queue_report(store, _OTHER_TASK)
    entry = store.guidance.pending("thread", _TARGET)[0]

    assert store.guidance.available_for_turn(
        entry, expected_turn_id=_MY_TASK, owning_task_id=_MY_TASK
    ) is False


def test_reachable_pending_input_agrees_with_claim(tmp_path) -> None:
    """两个判定必须一致：对每条投递，能不能认领与算不算待处理输入结论相同。"""
    store = _store(tmp_path)
    _queue_report(store, _OTHER_TASK)
    _queue_report(store, _MY_TASK)
    entries = {entry.guidance_id: entry for entry in store.guidance.pending("thread", _TARGET)}
    assert len(entries) == 2

    for entry in entries.values():
        available = store.guidance.available_for_turn(
            entry, expected_turn_id=_MY_TASK, owning_task_id=_MY_TASK
        )
        # 只有属于本回合（编号一致）的那条算待处理；另一条不算。
        expected = (
            str((entry.metadata or {}).get("session_task_id") or "").strip() == _MY_TASK
        )
        assert available is expected


def test_has_pending_request_guidance_ignores_other_task_report(tmp_path) -> None:
    """运行循环真正问的那个入口也必须返回 False（否则每次回复都被作废）。"""
    store = _store(tmp_path)
    _queue_report(store, _OTHER_TASK)
    agent = _agent(store)
    params = _params(store)

    assert has_pending_request_guidance(agent, params) is False


def test_has_pending_request_guidance_sees_own_task_report(tmp_path) -> None:
    """自己的待处理内容仍要看得到（不能一刀切把回报都挡掉）。"""
    store = _store(tmp_path)
    _queue_report(store, _MY_TASK)
    agent = _agent(store)
    params = _params(store)

    assert has_pending_request_guidance(agent, params) is True


def test_invalidation_counter_stops_after_the_limit() -> None:
    """结构化兜底：同一回合的作废次数超过上限就报告应该停止。"""
    params = SimpleNamespace(live_archive_state={})

    results = [
        _pending_turn_input_invalidation_exhausted(params)
        for _ in range(PENDING_TURN_INPUT_INVALIDATION_COUNT + 5)
    ]

    assert results[:PENDING_TURN_INPUT_INVALIDATION_COUNT] == [False] * PENDING_TURN_INPUT_INVALIDATION_COUNT
    assert results[PENDING_TURN_INPUT_INVALIDATION_COUNT] is True
    # 上限后的每一次都为真——循环会立刻停，不会继续试。
    assert all(results[PENDING_TURN_INPUT_INVALIDATION_COUNT:])


def test_invalidation_counter_is_per_turn(tmp_path) -> None:
    """计数按回合隔离：新回合不继承上一回合的作废次数。"""
    first = SimpleNamespace(live_archive_state={})
    for _ in range(PENDING_TURN_INPUT_INVALIDATION_COUNT + 1):
        _pending_turn_input_invalidation_exhausted(first)

    second = SimpleNamespace(live_archive_state={})

    assert _pending_turn_input_invalidation_exhausted(second) is False
