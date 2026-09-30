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

from agent_py_agent.agent.backends.errors import ProviderConnectionError, ProviderTransientError
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


# 会让回合崩溃的失败（唤醒毒丸计数的程序错误）：只有这类失败计入释放上限。
_CRASH = RuntimeError("回合崩溃")


# 函数用途: 模拟回合没消费就结束：Gateway 收尾对这一回合 reject_pending(reject_reserved=True)；failure 是回合抛出的异常，
#   默认是计次的程序错误。
def _end_turn(store: ConversationStore, turn_id: str, *, failure: BaseException | None = _CRASH) -> dict:
    return store.guidance.recovery.reject_pending(turn_id, reject_reserved=True, failure=failure)


# 函数用途: 写一条派活正文回执：metadata 带它所属的任务号（认领时按它核对归属）。
def _append_task_body(store: ConversationStore, key: str, task_id: str):
    return store.guidance.append_once(
        {"target_type": "thread", "target_id": _TARGET, "message": f"任务正文 {key}", "sender": _SENDER,
         "priority": "normal", "delivery": "next_turn",
         "metadata": {"origin_kind": SESSION_TASK_ORIGIN_KIND, "origin_thread_id": _SENDER, "session_task_id": task_id}},
        dedupe_key=key,
    )


# 函数用途: 以某个回合、某个归属任务号认领派活正文（前台回合没有归属任务号）。
def _claim_body(store: ConversationStore, key: str, turn_id: str, owner: str) -> bool:
    receipt = store.guidance.receipt(key)
    return store.guidance.claim_for_turn(
        receipt.entry, expected_turn_id=turn_id, attempt_id=f"attempt-{turn_id}", owning_task_id=owner,
    )


# 函数用途: 模拟后台片异常结束的收尾：非取消时允许派活正文退回（只给同一回合号的重跑），取消时不退；默认是计次的程序错误。
def _end_background_turn(store: ConversationStore, turn_id: str, *, cancelled: bool = False) -> dict:
    return store.guidance.recovery.reject_pending(
        turn_id, reject_reserved=True, release_task_body=not cancelled, failure=_CRASH,
    )


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


@pytest.mark.parametrize(("origin_kind", "background"), [
    ("", False), (SESSION_TASK_ORIGIN_KIND, False), ("", True),
], ids=["steer", "session-task-body", "steer-background-failure"])
def test_other_receipts_are_still_rejected(tmp_path, origin_kind, background) -> None:
    """插话照旧 rejected（由 Gateway 输入对账重新排成请求），后台片非取消的失败（release_task_body=True）也一样——
    这个开关只放行派活正文；前台终态下派活正文不释放（任务被取消后释放会让前台回合认领到它）。"""
    store = ConversationStore(tmp_path / "conv")
    entry = _append(store, "other-1", origin_kind=origin_kind)
    assert store.guidance.claim_for_turn(entry, expected_turn_id="turn-1", attempt_id="attempt-1")
    if background:
        _end_background_turn(store, "turn-1")
    else:
        _end_turn(store, "turn-1")
    receipt = store.guidance.receipt("other-1")
    assert receipt.status == "rejected" and "released_turn_ids" not in receipt.migration


@pytest.mark.parametrize("same_turn", [False, True], ids=["next-turns", "same-wake-rerun"])
def test_release_stops_at_the_limit_with_a_structured_code(tmp_path, same_turn) -> None:
    """一条会让回合崩溃的消息不能无限循环：释放满上限后再没消费就结束，回执转 rejected 并记结构化原因码。

    释放按次计数，不按回合去重：同一条唤醒重跑用的是同一个回合号（wake_signal_id），按回合去重的话它反复失败
    永远到不了上限。
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


@pytest.mark.parametrize(("raised", "cancelled", "expected"), [
    (RuntimeError("模型调用失败"), False, {"reject_reserved": True, "release_task_body": True}),
    (InterruptedError("任务已取消"), True, {"reject_reserved": True, "release_task_body": False}),
    (BackgroundCompactSliceYield("Compact 公平让出"), False, None),
], ids=["failed", "task-cancelled", "compact-yield"])
def test_background_slice_settles_its_turn_input_only_on_abnormal_end(
    tmp_path, monkeypatch, raised, cancelled, expected,
) -> None:
    """后台片异常结束时按这一片的精确回合号（消息唤醒取 wake_signal_id）走同一条收尾；任务已取消（结构化任务状态）时
    不退回派活正文。Compact 公平让出是同一回合换片续跑，预留已退回 pending 等续跑认领，这时收尾会把派活正文和插话
    误判为 rejected。走真实链路的后台调度器。"""
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
    monkeypatch.setattr(runtime_module, "_session_task_turn_was_cancelled", lambda *_args: cancelled)
    try:
        chain.scheduler.tick(now=time.time())
    except RuntimeError:
        pass
    calls = [call for call in settled if call[0] == wake.wake_signal_id]
    # 收尾把这一片抛出的异常原样交给 reject_pending，由它按唤醒毒丸的分类决定计不计次。
    assert calls == ([(wake.wake_signal_id, {**expected, "failure": raised})] if expected else [])


def test_task_body_goes_back_only_to_its_own_task_turn_after_a_failed_background_slice(tmp_path) -> None:
    """派活片非取消的失败：正文退回 pending、按次计数，但不授权跨回合——前台回合、别的任务回合、归属任务号对不上的
    认领都拿不到；只有同一个任务号的重跑能认领，重跑再失败时收尾还能按回合索引找到它（读回执时投影修复补回）。"""
    store = ConversationStore(tmp_path / "conv")
    _append_task_body(store, "body-1", "task-1")
    assert _claim_body(store, "body-1", "task-1", "task-1")
    assert _end_background_turn(store, "task-1")["released"] == 1
    receipt = store.guidance.receipt("body-1")
    assert (receipt.status, receipt.migration["release_count"]) == ("pending", 1)
    assert "released_turn_ids" not in receipt.migration, "派活正文的退回不能授权跨回合认领"
    assert store.guidance.available_for_turn(receipt.entry, expected_turn_id="turn-foreground") is False
    assert not _claim_body(store, "body-1", "turn-foreground", ""), "前台回合认领到了退回的派活正文"
    assert not _claim_body(store, "body-1", "task-2", "task-2"), "别的任务回合认领到了退回的派活正文"
    assert not _claim_body(store, "body-1", "task-1", "task-2"), "归属任务号对不上也认领到了"
    assert _claim_body(store, "body-1", "task-1", "task-1"), "同一任务号的重跑应当能认领"
    assert _turn_index(store, "task-1", "body-1").exists()


def test_cancelled_task_body_is_not_released(tmp_path) -> None:
    """任务已取消：派活正文照旧 rejected，不退回（退回会让之后的回合认领到已取消任务的正文）。"""
    store = ConversationStore(tmp_path / "conv")
    _append_task_body(store, "body-1", "task-1")
    assert _claim_body(store, "body-1", "task-1", "task-1")
    _end_background_turn(store, "task-1", cancelled=True)
    receipt = store.guidance.receipt("body-1")
    assert receipt.status == "rejected" and "release_count" not in receipt.migration


def test_task_body_release_stops_at_the_limit_with_a_structured_code(tmp_path) -> None:
    """同一派活片反复失败：退回次数沿用 release_count 上限，满上限后再失败转 rejected 并记结构化原因码。"""
    store = ConversationStore(tmp_path / "conv")
    _append_task_body(store, "body-1", "task-1")
    for index in range(1, SESSION_MESSAGE_RELEASE_LIMIT + 1):
        assert _claim_body(store, "body-1", "task-1", "task-1"), f"第 {index} 次重跑应当能认领"
        _end_background_turn(store, "task-1")
        released = store.guidance.receipt("body-1")
        assert (released.status, released.migration["release_count"]) == ("pending", index)
    assert _claim_body(store, "body-1", "task-1", "task-1")
    _end_background_turn(store, "task-1")
    receipt = store.guidance.receipt("body-1")
    assert receipt.status == "rejected"
    assert receipt.migration["rejection_code"] == SESSION_MESSAGE_RELEASE_LIMIT_REACHED
    assert not _claim_body(store, "body-1", "task-1", "task-1"), "转 rejected 后不再能认领"


@pytest.mark.parametrize("failure", [
    None, TimeoutError("超时"), InterruptedError("用户 /stop 或取消"), ProviderTransientError("429 或连接断开"),
    ProviderConnectionError("代理配置错误"),
], ids=["no-exception", "timeout", "stop-or-cancel", "provider-transient", "provider-environment"])
def test_failures_the_poison_does_not_count_never_drop_the_message(tmp_path, failure) -> None:
    """释放照常，但只有唤醒毒丸会计数的失败才计次（两层同一个判据 verdict_for_error）：超时、429、连接、环境故障、
    用户 /stop 与取消、没有异常，连续多少次都只释放、不计次、不转 rejected；之后一次会计数的失败也只计第 1 次。"""
    store = ConversationStore(tmp_path / "conv")
    _append(store, "msg-1")
    for index in range(1, SESSION_MESSAGE_RELEASE_LIMIT + 3):
        assert _claim(store, "msg-1", f"turn-{index}"), f"第 {index} 回合应当能认领"
        _end_turn(store, f"turn-{index}", failure=failure)
        receipt = store.guidance.receipt("msg-1")
        assert receipt.status == "pending" and "release_count" not in receipt.migration
    assert _claim(store, "msg-1", "turn-crash")
    _end_turn(store, "turn-crash")
    assert store.guidance.receipt("msg-1").migration["release_count"] == 1


@pytest.mark.parametrize(("failure", "counted"), [
    (RuntimeError("程序错误"), True), (None, False), (TimeoutError("超时"), False), (InterruptedError("用户 /stop 或取消"), False),
    (ProviderTransientError("429 或连接断开"), False), (ProviderConnectionError("代理配置错误"), False),
], ids=["program-error", "no-exception", "timeout", "stop-or-cancel", "provider-transient", "provider-environment"])
@pytest.mark.parametrize("source", ["session-message", "task-body"])
def test_release_counting_is_the_same_for_messages_and_task_bodies(tmp_path, source, failure, counted) -> None:
    """会话消息与后台失败退回的派活正文用同一判据计次（ae 的变异 A4：派活正文一支总是计次时原有用例抓不到）。
    同一条回执连续 6 次没消费就结束：计次的失败在第 6 次转 rejected 带码；不计次的结束一直 pending、不写次数。"""
    store = ConversationStore(tmp_path / "conv")
    if source == "session-message":
        _append(store, "key-1")

        def claim(index: int) -> bool:
            return _claim(store, "key-1", f"turn-{index}")

        def end(index: int) -> None:
            _end_turn(store, f"turn-{index}", failure=failure)
    else:
        _append_task_body(store, "key-1", "task-1")

        def claim(index: int) -> bool:
            return _claim_body(store, "key-1", "task-1", "task-1")

        def end(index: int) -> None:
            store.guidance.recovery.reject_pending("task-1", reject_reserved=True, release_task_body=True, failure=failure)
    for index in range(1, SESSION_MESSAGE_RELEASE_LIMIT + 2):
        assert claim(index), f"第 {index} 次应当能认领"
        end(index)
    receipt = store.guidance.receipt("key-1")
    if counted:
        assert (receipt.status, receipt.migration.get("rejection_code")) == ("rejected", SESSION_MESSAGE_RELEASE_LIMIT_REACHED)
        assert receipt.migration["release_count"] == SESSION_MESSAGE_RELEASE_LIMIT
    else:
        assert receipt.status == "pending" and "release_count" not in receipt.migration


def test_only_counted_failures_reach_the_limit_even_when_interleaved(tmp_path) -> None:
    """计次的失败与不计次的结束交替出现：计满上限后，不计次的结束照样只释放；再来一次计次的失败才转 rejected。"""
    store = ConversationStore(tmp_path / "conv")
    _append(store, "msg-1")
    turns = iter(range(1, 100))
    for _index in range(SESSION_MESSAGE_RELEASE_LIMIT):
        for failure in (_CRASH, TimeoutError("超时")):
            turn = f"turn-{next(turns)}"
            assert _claim(store, "msg-1", turn)
            _end_turn(store, turn, failure=failure)
    receipt = store.guidance.receipt("msg-1")
    assert (receipt.status, receipt.migration["release_count"]) == ("pending", SESSION_MESSAGE_RELEASE_LIMIT)
    turn = f"turn-{next(turns)}"
    assert _claim(store, "msg-1", turn)
    _end_turn(store, turn)
    assert store.guidance.receipt("msg-1").migration.get("rejection_code") == SESSION_MESSAGE_RELEASE_LIMIT_REACHED
