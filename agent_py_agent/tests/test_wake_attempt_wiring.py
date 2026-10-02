"""唤醒毒丸第 3 步 C3：后台唤醒车道真的按尝试记账、退避、隔离与结案。

走真实链路：真实 SimpleAgent、真实会话存储与尝试账、Gateway 同款后台调度器，只有供应商传输是替身（test_session_task_real_chain
的 RealChain）。A 给空闲的 B 发一条消息，得到一条 B 的会话消息唤醒；再往这条唤醒的执行路径里注入故障（领取后准入返回
未知码、run_once 抛程序错误或瞬时错误），逐拍推进时钟，核对尝试账、结案记录和"之后不再领取"。不联网、不调用真实模型。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.conversation import FakeDeliveryService
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation import wake_attempt_tracking as tracking
from agent_py_agent.agent.conversation.background_claim import SESSION_MESSAGE_CONSUMED_ADMISSION
from agent_py_agent.agent.conversation.session_messaging import SESSION_MESSAGE_KEY_FIELD
from agent_py_agent.agent.conversation.store_wake_attempts import WakeAttemptStart
from agent_py_agent.agent.conversation.wake_attempt_tracking import (
    inflight_attempts,
    mark_inflight_attempts_stopping,
    wake_attempt_deferred,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT_COUNT,
    WAKE_REASON_GATEWAY_STOPPED,
    WAKE_REASON_LEDGER_CORRUPT,
)
from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.cli.gateway_loops import _build_background_scheduler
from agent_py_agent.tests import test_session_task_real_chain as rc


# 函数用途: 搭真实链路并让 A 给空闲的 B 发一条消息，返回链路和 B 的那条会话消息唤醒。
def _chain_with_message_wake(tmp_path, monkeypatch, *, count: int = 1):
    chain = rc._real_chain(tmp_path, monkeypatch)
    for index in range(count):
        chain.ask("A", f"RC-MESSAGE {chain.threads['B']} RC-NOTE-W{index} 你好 B。")
    wakes = _b_wakes(chain)
    assert len(wakes) == count, f"前提不成立：B 应当有 {count} 条待处理的消息唤醒，实际 {len(wakes)}"
    return chain, wakes


def _b_wakes(chain) -> list:
    return [wake for wake in chain.agent.conversation_store.wakes.pending(limit=0) if wake.thread_id == chain.threads["B"]]


def _attempts(chain):
    return chain.agent.conversation_store.wakes.attempts


def _state(chain, wake):
    state, error = _attempts(chain).state_report(wake.wake_signal_id)
    assert error is None, error
    return state


# 函数用途: 让领取后的来源准入对所有唤醒都返回给定码（不开模型回合）。
def _inject_admission(monkeypatch, code: str) -> None:
    monkeypatch.setattr(runtime_module, "_background_wake_source_admission", lambda *_args, **_kwargs: code)


# 函数用途: 记下后台 claim 的每次结算（领到租约才会结算），用来断言"之后不再领取"。
def _spy_claims(chain, monkeypatch) -> list[dict]:
    finished: list[dict] = []
    original = chain.scheduler.store.claims.finish

    def spy(payload):
        finished.append(dict(payload))
        return original(payload)

    monkeypatch.setattr(chain.scheduler.store.claims, "finish", spy)
    return finished


# 函数用途: 在这条唤醒的持久退避到期之后推进一拍（时间戳取真实时间之后，保证跳过判定放行）。
def _tick_after_backoff(chain, wake) -> None:
    state, _error = _attempts(chain).state_report(wake.wake_signal_id)
    chain.scheduler.tick(now=max(time.time(), state.next_attempt_at) + 1)


def test_unknown_admission_is_quarantined_after_the_same_cause_limit(tmp_path, monkeypatch) -> None:
    """未知准入码按计数失败记：退避 30/60/120/240 秒，第 5 次同因结案，之后不再领取；退避写在账里，换一个调度器实例照样生效。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    claims = _spy_claims(chain, monkeypatch)
    gaps = []
    for count in range(1, WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
        _tick_after_backoff(chain, wake)
        state = _state(chain, wake)
        assert (state.reason_code, state.same_cause_count) == ("admission:host_delivery_consumed", count)
        gaps.append(round(state.next_attempt_at - state.last_failed_at))
        if count == 1:
            # 退避期内不领取：同一个调度器，以及模拟重启后的新调度器实例都跳过。
            before = len(claims)
            chain.scheduler.tick(now=time.time() + 1)
            _build_background_scheduler(chain.agent, FakeDeliveryService()).tick(now=time.time() + 1)
            assert len(claims) == before, "持久退避期内又领取了"
    assert gaps == [30, 60, 120, 240]
    _tick_after_backoff(chain, wake)
    assert _b_wakes(chain) == [], "第 5 次同因失败后应当结案，离开待处理队列"
    rows, errors = _attempts(chain).quarantined()
    assert errors == [] and [(row["wake_signal_id"], row["reason_code"]) for row in rows] == [
        (wake.wake_signal_id, "admission:host_delivery_consumed")]
    before = len(claims)
    chain.scheduler.tick(now=time.time() + 10_000)
    assert len(claims) == before, "结案后不再领取"


def test_program_error_in_the_slice_is_counted_and_quarantined(tmp_path, monkeypatch) -> None:
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)

    def broken(_request):
        raise RuntimeError("程序错误")

    monkeypatch.setattr(chain.scheduler.runtime, "run_once", broken)
    for _index in range(WAKE_POISON_SAME_CAUSE_LIMIT_COUNT):
        with pytest.raises(RuntimeError):
            _tick_after_backoff(chain, wake)
    rows, _errors = _attempts(chain).quarantined()
    assert [row["wake_signal_id"] for row in rows] == [wake.wake_signal_id]
    assert rows[0]["reason_code"].endswith(":RuntimeError")


def test_transient_provider_failures_are_never_quarantined(tmp_path, monkeypatch) -> None:
    """瞬时供应错误 20 次：不计数（只推进连续不计数段），不结案；供应冷却吸收异常，tick 不抛。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)

    def flaky(_request):
        raise ProviderTransientError("503")

    monkeypatch.setattr(chain.scheduler.runtime, "run_once", flaky)
    base = time.time()
    for index in range(20):
        chain.scheduler.tick(now=base + 1000 * (index + 1))
    state = _state(chain, wake)
    assert (state.total_count, state.uncounted_count) == (0, 20)
    assert [item.wake_signal_id for item in _b_wakes(chain)] == [wake.wake_signal_id]


def test_success_after_failures_clears_the_ledger(tmp_path, monkeypatch) -> None:
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    for _index in range(2):
        _tick_after_backoff(chain, wake)
    assert _state(chain, wake).same_cause_count == 2
    monkeypatch.undo()
    # monkeypatch.undo 也撤掉了假线路，重新装上；必须经 monkeypatch 登记，测试结束才会还原，否则假线路泄漏给后续测试。
    monkeypatch.setattr(rc.http, "post_json", chain.wire)
    _tick_after_backoff(chain, wake)
    assert _b_wakes(chain) == [] and not _attempts(chain).has_ledger(wake.wake_signal_id)


def test_wake_settled_elsewhere_drops_its_ledger(tmp_path, monkeypatch) -> None:
    """唤醒被来源已处理完的准入结案（经 retire_source）后，尝试结束时它已不在 pending：账直接删除，不再记账。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    _tick_after_backoff(chain, wake)
    assert _attempts(chain).has_ledger(wake.wake_signal_id)
    _inject_admission(monkeypatch, SESSION_MESSAGE_CONSUMED_ADMISSION)
    _tick_after_backoff(chain, wake)
    assert _b_wakes(chain) == [] and not _attempts(chain).has_ledger(wake.wake_signal_id)


def test_batch_failure_isolates_members_without_counting(tmp_path, monkeypatch) -> None:
    """两条同批的唤醒一起失败：不计数，只记批次失败；下一次各自单独执行，才按单条计数。"""
    chain, wakes = _chain_with_message_wake(tmp_path, monkeypatch, count=2)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    chain.scheduler.tick(now=time.time() + 1)
    states = [_state(chain, wake) for wake in wakes]
    assert [(state.batch_failures, state.total_count) for state in states] == [(1, 0), (1, 0)]
    chain.scheduler.tick(now=max(state.next_attempt_at for state in states) + 1)
    states = [_state(chain, wake) for wake in wakes]
    assert [(state.batch_failures, state.total_count) for state in states] == [(1, 1), (1, 1)]


def test_ready_scan_agrees_with_the_skip_stage(tmp_path, monkeypatch) -> None:
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    _tick_after_backoff(chain, wake)
    due = _state(chain, wake).next_attempt_at
    assert chain.threads["B"] not in chain.scheduler.ready_thread_ids(now=due - 1)
    assert chain.threads["B"] in chain.scheduler.ready_thread_ids(now=due + 1)


# 函数用途: 一个已经退出的子进程的身份（本机、pid 已死），用来伪造"上次在途的进程已死"。
def _dead_process_identity() -> dict:
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    return build_process_identity(int(child.stdout.strip()))


# 函数用途: 把这条唤醒的尝试账改成"有一次在途尝试，属于已死进程"（可带停机标记）。
def _plant_dead_in_flight(chain, wake, *, stopping: bool) -> None:
    path = chain.agent.conversation_store.storage.wake_attempt_path(wake.wake_signal_id)
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger["in_flight"] = {"claim_id": "claim-dead", "batch_size": 1, "owner_process": _dead_process_identity(),
                           "started_at": time.time() - 5, **({"stopping_at": time.time() - 4} if stopping else {})}
    path.write_text(json.dumps(ledger), encoding="utf-8")


@pytest.mark.parametrize("stopping", [False, True], ids=["killed", "graceful-stop"])
def test_skip_stage_settles_an_attempt_whose_process_died(tmp_path, monkeypatch, stopping) -> None:
    """跳过阶段发现上次在途的进程已死：被强杀记一次计数的 abandoned；优雅停机打过标记的记不计数。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    _inject_admission(monkeypatch, "host_delivery_consumed")
    _tick_after_backoff(chain, wake)
    _plant_dead_in_flight(chain, wake, stopping=stopping)
    wake_attempt_deferred(chain.scheduler, wake, time.time())
    state = _state(chain, wake)
    if stopping:
        assert (state.total_count, state.uncounted_reason_code) == (1, WAKE_REASON_GATEWAY_STOPPED)
    else:
        assert (state.total_count, state.reason_code) == (2, "attempt:abandoned")


def test_corrupt_ledger_is_quarantined_before_running(tmp_path, monkeypatch) -> None:
    """尝试账读不出：跳过阶段直接按 attempt:ledger_corrupt 结案，坏账原样留档，这条唤醒不再执行（不清零重来）。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    path = chain.agent.conversation_store.storage.wake_attempt_path(wake.wake_signal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{truncated", encoding="utf-8")
    calls_before = len(chain.wire.calls)
    claims = _spy_claims(chain, monkeypatch)
    chain.scheduler.tick(now=time.time() + 1)
    assert _b_wakes(chain) == [] and len(chain.wire.calls) == calls_before
    assert claims == [], "跳过阶段就应结案，不必先领租约再在 begin 里拦下"
    rows, _errors = _attempts(chain).quarantined()
    assert [(row["wake_signal_id"], row["reason_code"]) for row in rows] == [(wake.wake_signal_id, WAKE_REASON_LEDGER_CORRUPT)]


def test_ledger_write_failure_does_not_mask_the_slice_error(tmp_path, monkeypatch) -> None:
    """记账放在 finally 里：执行抛错且记账自己也写不进去时，原异常照样抛出，不被记账异常遮住。"""
    chain, (_wake,) = _chain_with_message_wake(tmp_path, monkeypatch)

    def broken(_request):
        raise RuntimeError("程序错误")

    def unwritable(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(chain.scheduler.runtime, "run_once", broken)
    monkeypatch.setattr(_attempts(chain), "record", unwritable)
    with pytest.raises(RuntimeError, match="程序错误"):
        chain.scheduler.tick(now=time.time() + 1)


def test_error_outside_the_slice_is_still_counted(tmp_path, monkeypatch) -> None:
    """执行函数之外、这批唤醒处理过程中抛出的异常（这里是合批一步）同样记为这次尝试的结果：没领到租约但确实失败了。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)

    def broken(_signals):
        raise RuntimeError("合批出错")

    monkeypatch.setattr(runtime_module, "_batched_wake_signal", broken)
    with pytest.raises(RuntimeError, match="合批出错"):
        chain.scheduler.tick(now=time.time() + 1)
    state = _state(chain, wake)
    assert state.total_count == 1 and state.reason_code.endswith(":RuntimeError")


@pytest.mark.parametrize("kind", ["message", "task"])
def test_in_flight_record_carries_the_slice_turn_id(tmp_path, monkeypatch, kind) -> None:
    """在途记录持久化这一片的回合号（与注入补充消息时同源）：会话消息唤醒取 wake_signal_id，派活取 session_task_id。
    进程中途死亡时 C6 只能从账里读到它，进程内的在途登记会随旧进程消失。"""
    chain = rc._real_chain(tmp_path, monkeypatch)
    if kind == "message":
        chain.ask("A", f"RC-MESSAGE {chain.threads['B']} RC-NOTE-TURN 你好 B。")
    else:
        chain.ask("A", f"RC-DISPATCH {chain.threads['B']} RC-GOAL-DONE 整理三条要点。")
    (wake,) = _b_wakes(chain)
    expected = wake.wake_signal_id if kind == "message" else chain.task("RC-GOAL-DONE").task_id
    seen: dict = {}

    def capture(_request):
        path = chain.agent.conversation_store.storage.wake_attempt_path(wake.wake_signal_id)
        seen["in_flight"] = json.loads(path.read_text(encoding="utf-8"))["in_flight"]
        seen["registered"] = [(row.wake_signal_id, row.turn_id) for row in inflight_attempts()]
        raise RuntimeError("观察完在途记录后让这一片失败")

    monkeypatch.setattr(chain.scheduler.runtime, "run_once", capture)
    with pytest.raises(RuntimeError):
        chain.scheduler.tick(now=time.time())
    assert seen["in_flight"]["turn_id"] == expected
    assert (wake.wake_signal_id, expected) in seen["registered"]


def test_frozen_redelivery_failures_back_off_and_close_after_a_day(tmp_path) -> None:
    """冻结交付的唤醒只重投：每次重投失败都记成只重投失败（不计同因），按只重投退避往后推，不再调模型；首次重投失败满 24 小时后
    按 delivery:channel_unavailable 结案、不再领取。ae 复审 C3 的 F2：只重投路径标错时整次尝试不记账，退避和 24 小时结案都不生效。"""
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.conversation.wake_poison import (
        WAKE_REASON_CHANNEL_UNAVAILABLE,
        WAKE_REDELIVERY_GIVE_UP_SECONDS,
        redelivery_backoff_seconds,
    )
    from agent_py_agent.tests import test_background_owner_delivery_commit as owner

    agent, backend = owner._agent(tmp_path, "阶段汇报：还在等子代理。")
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "owner-im", "channel": "feishu",
                                          "channel_conversation_id": "chat-im", "channel_user_id": "open-id-im", "now": 1.0})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-im", "goal": "已有任务的后台汇报", "status": "active"})
    channels = owner._ScriptedDelivery(proactive=True, status="rejected")
    scheduler = BackgroundMainAgentScheduler(
        {"runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels), "store": store})
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "scheduled_progress_report",
                                       "root_task_id": "task-im", "now": 2.0})
    scheduler.tick(now=20.0)
    _require_frozen = store.wakes.pending_one(signal.wake_signal_id)
    assert _require_frozen is not None and "owner_delivery" in _require_frozen.metadata, "前提不成立：答复应已冻结"
    attempts = store.wakes.attempts
    state, error = attempts.state_report(signal.wake_signal_id)
    assert error is None and (state.redelivery_failures, state.same_cause_count) == (1, 0)
    first = state.first_redelivery_failed_at
    for failures in range(2, 5):
        scheduler.tick(now=state.next_attempt_at + 1)
        state, _error = attempts.state_report(signal.wake_signal_id)
        assert (state.redelivery_failures, state.same_cause_count) == (failures, 0), "只重投失败没有记账"
        assert round(state.next_attempt_at - state.last_redelivery_failed_at) == redelivery_backoff_seconds(failures)
    assert len(backend.prompts) == 1, "只重投不得再调模型"
    scheduler.tick(now=first + WAKE_REDELIVERY_GIVE_UP_SECONDS + 1)
    assert store.wakes.pending_one(signal.wake_signal_id) is None, "首次重投失败满 24 小时应当结案"
    rows, errors = attempts.quarantined()
    assert errors == [] and [row["reason_code"] for row in rows] == [WAKE_REASON_CHANNEL_UNAVAILABLE]
    sent = len(channels.sent)
    scheduler.tick(now=first + WAKE_REDELIVERY_GIVE_UP_SECONDS + 10_000)
    assert len(channels.sent) == sent, "结案后不再重投"


# 函数用途: 给这条唤醒写一次在途尝试（带回合号），再把它的进程身份改成已死进程，伪造"那一片连同进程一起没了"。
def _begin_then_kill(chain, wake, *, turn_id: str) -> None:
    _attempts(chain).begin(wake, WakeAttemptStart("claim-dead", turn_id=turn_id), now=time.time())
    path = chain.agent.conversation_store.storage.wake_attempt_path(wake.wake_signal_id)
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger["in_flight"]["owner_process"] = _dead_process_identity()
    path.write_text(json.dumps(ledger), encoding="utf-8")


@pytest.mark.parametrize("turn_known", [True, False], ids=["turn-id-persisted", "old-ledger-without-turn-id"])
def test_a_message_claimed_by_a_dead_slice_goes_back_and_is_delivered(tmp_path, monkeypatch, turn_known) -> None:
    """C6：进程在一片里认领了会话消息后死掉；下一次跳过阶段按尝试账里持久化的回合号把它退回 pending（不计次），
    之后唤醒回合照常领取、消息正常送到；旧账没有回合号时不收尾，回执保持原状。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    guidance = chain.agent.conversation_store.guidance
    key = str(wake.metadata.get(SESSION_MESSAGE_KEY_FIELD) or "")
    turn_id = runtime_module._wake_turn_id(wake)
    assert key and turn_id, "前提不成立：消息唤醒应带消息键和回合号"
    assert guidance.claim_for_turn(guidance.receipt(key).entry, expected_turn_id=turn_id, attempt_id="attempt-dead")
    assert guidance.receipt(key).status == "reserved"
    _begin_then_kill(chain, wake, turn_id=turn_id if turn_known else "")

    wake_attempt_deferred(chain.scheduler, wake, time.time())

    receipt = guidance.receipt(key)
    if not turn_known:
        assert receipt.status == "reserved"
        return
    assert receipt.status == "pending" and receipt.migration["released_turn_ids"] == [turn_id]
    assert "release_count" not in receipt.migration, "进程死亡的退回不计次"
    _tick_after_backoff(chain, wake)
    assert guidance.receipt(key).status == "consumed"


def test_shutdown_marks_in_flight_attempts_and_isolates_write_failures(tmp_path, monkeypatch) -> None:
    """C6：停机前给本进程在途的尝试写停机标记；某一条写失败只打日志、其余照写，整体绝不抛出。"""
    chain, wakes = _chain_with_message_wake(tmp_path, monkeypatch, count=2)
    attempts = _attempts(chain)
    starts = {wake.wake_signal_id: WakeAttemptStart(f"claim-{index}") for index, wake in enumerate(wakes)}
    for wake in wakes:
        attempts.begin(wake, starts[wake.wake_signal_id], now=time.time())
        tracking._register_inflight(attempts, wake.wake_signal_id, starts[wake.wake_signal_id])
    real = attempts.mark_stopping

    def flaky(wake_signal_id, claim_id, *, now):
        if wake_signal_id == wakes[0].wake_signal_id:
            raise OSError("disk full")
        return real(wake_signal_id, claim_id, now=now)

    monkeypatch.setattr(attempts, "mark_stopping", flaky)
    try:
        assert mark_inflight_attempts_stopping(now=123.0) == 1
    finally:
        for wake in wakes:
            tracking._unregister_inflight(attempts, wake.wake_signal_id)
    path = chain.agent.conversation_store.storage.wake_attempt_path(wakes[1].wake_signal_id)
    assert json.loads(path.read_text(encoding="utf-8"))["in_flight"]["stopping_at"] == 123.0


def test_supervisor_shutdown_marks_in_flight_attempts_before_closing_the_pools(monkeypatch) -> None:
    """C6：后台 supervisor 停机时先打停机标记，再关两个执行池。"""
    from agent_py_agent.cli import gateway_loops

    order: list[str] = []
    monkeypatch.setattr(tracking, "mark_inflight_attempts_stopping", lambda *, now: order.append("mark") or 0)

    class _Pool:
        def shutdown(self, **_kwargs):
            order.append("pool")

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._executor, supervisor._curator_executor = _Pool(), _Pool()
    supervisor.shutdown()
    assert order == ["mark", "pool", "pool"]


def test_begin_settles_a_dead_slice_that_preflight_did_not_see(tmp_path, monkeypatch) -> None:
    """C6：preflight 与 begin 之间的竞态由 begin 兜住：begin 识别出上一片的进程已死时同样按回合号收尾，新的一片照常开始。"""
    chain, (wake,) = _chain_with_message_wake(tmp_path, monkeypatch)
    guidance = chain.agent.conversation_store.guidance
    key = str(wake.metadata.get(SESSION_MESSAGE_KEY_FIELD) or "")
    turn_id = runtime_module._wake_turn_id(wake)
    assert guidance.claim_for_turn(guidance.receipt(key).entry, expected_turn_id=turn_id, attempt_id="attempt-dead")
    _begin_then_kill(chain, wake, turn_id=turn_id)
    tracker = tracking.WakeAttemptTracker(chain.scheduler, (wake,), time.time(), turn_id=turn_id)
    try:
        assert tracker.begin("claim-next") == ""
        assert guidance.receipt(key).status == "pending"
    finally:
        tracking._unregister_inflight(_attempts(chain), wake.wake_signal_id)
