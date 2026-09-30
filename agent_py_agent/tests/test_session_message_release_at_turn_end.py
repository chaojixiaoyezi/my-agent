"""目标回合没消费就结束时，会话消息释放给下一回合，不再永久 rejected（3a 裁定的方案 A）。

2026-09-29 真实链路实测：B 忙时收到消息、在安全点注入，带着它的调用返回前 B 被 /stop；回合收尾
reject_pending(reject_reserved=True) 把回执改成 rejected，之后没有任何回合认领它，内容只因被停回合留在历史里的
那段输入碰巧可见。方案 A：回合收尾对会话消息改为释放回 pending（migration.released_turn_ids 记下回合），
下一回合跨回合认领并改绑到自己名下；同一条消息释放到 SESSION_MESSAGE_RELEASE_LIMIT 次后转 rejected，
migration.rejection_code 记 SESSION_MESSAGE_RELEASE_LIMIT_REACHED。插话、派活正文、已提交的回执行为不变。
这里走真实 GuidanceStore，不启动 Gateway、不调用模型。
"""

from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.background_execution import BackgroundCompactSliceYield
from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGE_ORIGIN_KIND,
    SESSION_MESSAGE_RELEASE_LIMIT,
    SESSION_MESSAGE_RELEASE_LIMIT_REACHED,
    SESSION_TASK_ORIGIN_KIND,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_guidance_recovery import GuidanceRecovery

_TARGET = "thread-b"
_SENDER = "thread-a"


# 函数用途: 写一条回执：默认是会话消息；origin_kind 为空时是普通插话。
def _append(store: ConversationStore, key: str, *, origin_kind: str = SESSION_MESSAGE_ORIGIN_KIND):
    # 插话写入时就绑定它所属的活动回合；宿主投递（会话消息、派活正文）在认领时才补记。
    metadata = {"origin_kind": origin_kind, "origin_thread_id": _SENDER} if origin_kind else {"expected_turn_id": "turn-1"}
    return store.guidance.append_once(
        {"target_type": "thread", "target_id": _TARGET, "message": f"正文 {key}", "sender": _SENDER,
         "priority": "normal", "delivery": "next_turn", "metadata": metadata},
        dedupe_key=key,
    )


# 函数用途: 按回执当前绑定把它认领到给定回合（插话需要写入时就绑定回合，会话消息在认领时补记）。
def _claim(store: ConversationStore, key: str, turn_id: str) -> bool:
    receipt = store.guidance.receipt(key)
    return store.guidance.claim_for_turn(receipt.entry, expected_turn_id=turn_id, attempt_id=f"attempt-{turn_id}")


# 函数用途: 模拟回合没消费就结束：Gateway 收尾对这一回合 reject_pending(reject_reserved=True)。
def _end_turn(store: ConversationStore, turn_id: str) -> dict:
    return store.guidance.recovery.reject_pending(turn_id, reject_reserved=True)


# 函数用途: 某一回合在精确回合索引里有没有登记这条回执（回合收尾按它找本回合的消息）。
def _turn_index(store: ConversationStore, turn_id: str, key: str):
    return store.storage.guidance_turn_index_path(turn_id, key)


def test_unconsumed_session_message_is_released_to_the_next_turn(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conv")
    _append(store, "msg-1")
    assert _claim(store, "msg-1", "turn-1")
    assert _end_turn(store, "turn-1")["released"] == 1
    receipt = store.guidance.receipt("msg-1")
    assert receipt.status == "pending" and receipt.migration["released_turn_ids"] == ["turn-1"]
    # 下一回合能认领（能不能认领与算不算待处理输入同一条判据），认领时改绑到自己名下并补索引。
    assert store.guidance.available_for_turn(receipt.entry, expected_turn_id="turn-2") is True
    assert _claim(store, "msg-1", "turn-2")
    claimed = store.guidance.receipt("msg-1")
    assert claimed.status == "reserved" and claimed.entry.metadata["expected_turn_id"] == "turn-2"
    assert _turn_index(store, "turn-2", "msg-1").exists()


def test_submitted_session_message_is_not_released(tmp_path) -> None:
    """已提交（模型那边结果未知）的回执不释放：释放会让模型重复消费同一条消息。走真实安全点：认领→注入→提交。"""
    from agent_py_agent.tests import test_session_message_busy_target as busy

    store = busy._store(tmp_path)
    busy._bind_thread_task(store, busy._TARGET_THREAD)
    busy._write_message_body(store, "帮我看看这个接口")
    assert busy._inject_and_submit(store, busy._params()) == 1
    key = f"session_message:{busy._SENDER}->{busy._TARGET_THREAD}"
    assert store.guidance.receipt(key).status == "submitted"
    _end_turn(store, busy._TARGET_TURN)
    receipt = store.guidance.receipt(key)
    assert receipt.status == "submitted" and "released_turn_ids" not in receipt.migration


@pytest.mark.parametrize("origin_kind", ["", SESSION_TASK_ORIGIN_KIND], ids=["steer", "session-task-body"])
def test_other_receipts_are_still_rejected(tmp_path, origin_kind) -> None:
    """插话照旧 rejected（由 Gateway 输入对账重新排成请求）；派活正文不释放（任务被取消后释放会让前台回合认领到它）。"""
    store = ConversationStore(tmp_path / "conv")
    entry = _append(store, "other-1", origin_kind=origin_kind)
    assert store.guidance.claim_for_turn(entry, expected_turn_id="turn-1", attempt_id="attempt-1")
    _end_turn(store, "turn-1")
    receipt = store.guidance.receipt("other-1")
    assert receipt.status == "rejected" and "released_turn_ids" not in receipt.migration


@pytest.mark.parametrize("same_turn", [False, True], ids=["next-turns", "same-wake-rerun"])
def test_release_stops_at_the_limit_with_a_structured_code(tmp_path, same_turn) -> None:
    """一条会让回合崩溃的消息不能无限循环：释放满上限后再没消费就结束，回执转 rejected 并记结构化原因码。

    释放按次计数，不按回合去重：同一条唤醒重跑用的是同一个回合号（wake_signal_id），按回合去重的话它反复失败
    永远到不了上限。同一回合重新认领被释放的消息时也要补回这一回合的索引，否则下次收尾找不到它，消息又卡在 reserved。
    """
    store = ConversationStore(tmp_path / "conv")
    _append(store, "msg-1")
    turn = (lambda _index: "wake-1") if same_turn else (lambda index: f"turn-{index}")
    for index in range(1, SESSION_MESSAGE_RELEASE_LIMIT + 1):
        assert _claim(store, "msg-1", turn(index)), f"第 {index} 回合应当能认领"
        _end_turn(store, turn(index))
        released = store.guidance.receipt("msg-1")
        assert (released.status, released.migration["release_count"]) == ("pending", index)
    last = turn(SESSION_MESSAGE_RELEASE_LIMIT + 1)
    assert _claim(store, "msg-1", last)
    _end_turn(store, last)
    receipt = store.guidance.receipt("msg-1")
    assert receipt.status == "rejected"
    assert receipt.migration["rejection_code"] == SESSION_MESSAGE_RELEASE_LIMIT_REACHED
    assert receipt.migration["release_count"] == SESSION_MESSAGE_RELEASE_LIMIT
    assert len(receipt.migration["released_turn_ids"]) == (1 if same_turn else SESSION_MESSAGE_RELEASE_LIMIT)
    assert not _claim(store, "msg-1", "turn-after"), "转 rejected 后不再有回合能认领"


@pytest.mark.parametrize(("raised", "settles"), [
    (RuntimeError("模型调用失败"), True),
    (BackgroundCompactSliceYield("Compact 公平让出"), False),
], ids=["failed", "compact-yield"])
def test_background_slice_settles_its_turn_input_only_on_abnormal_end(tmp_path, monkeypatch, raised, settles) -> None:
    """后台片异常结束时按这一片的精确回合号（消息唤醒取 wake_signal_id）走同一条收尾；Compact 公平让出是同一回合
    换片续跑，预留已退回 pending 等续跑认领，这时收尾会把派活正文和插话误判为 rejected。走真实链路的后台调度器。"""
    from agent_py_agent.tests import test_session_task_real_chain as rc

    chain = rc._real_chain(tmp_path, monkeypatch)
    chain.ask("A", f"RC-MESSAGE {chain.threads['C']} RC-NOTE-SPY 你好 C。")
    (wake,) = [row for row in chain.agent.conversation_store.wakes.pending(limit=0) if row.thread_id == chain.threads["C"]]
    settled: list[tuple[str, dict]] = []
    original = GuidanceRecovery.reject_pending

    def spy(self, turn_id, **kwargs):
        settled.append((turn_id, kwargs))
        return original(self, turn_id, **kwargs)

    def invoke(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(GuidanceRecovery, "reject_pending", spy)
    monkeypatch.setattr(runtime_module, "_invoke_background_main_agent", invoke)
    try:
        chain.scheduler.tick(now=time.time())
    except RuntimeError:
        pass
    calls = [call for call in settled if call[0] == wake.wake_signal_id]
    assert calls == ([(wake.wake_signal_id, {"reject_reserved": True})] if settles else [])
