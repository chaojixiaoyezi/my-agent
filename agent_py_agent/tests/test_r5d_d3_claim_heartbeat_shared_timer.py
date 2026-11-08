"""D-1/D3: conversation claim heartbeats share one deferred-call thread and a durable renew pool instead of one
native thread per model turn."""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

from agent_py_agent.agent.conversation.run_claim import ConversationRunClaimHeartbeat

# 同时持有车道的回合数（每回合一只心跳）。
HEARTBEATS = 12
# 依次起停的批数；批数×每批心跳数必须大于共用线程数上限，旧实现才会显红。
ROUNDS = 4
# 心跳续约间隔（秒）。
INTERVAL_SECONDS = 0.05
# 每只心跳在被停之前至少续约的次数。
RENEWS_BEFORE_STOP = 2
# 等待续约/退出的上限（秒）。
WAIT_SECONDS = 3.0


# 类用途: 记录每个 claim 的续约次数；可让续约返回 None、抛错或阻塞到放行。
class _Claims:
    def __init__(self, result=lambda payload: {"claim_id": payload["claim_id"]}) -> None:
        self.result, self.calls, self.lock = result, [], threading.Lock()

    # 函数用途: 记录一次续约并按设定返回。
    def renew(self, payload: dict):
        with self.lock:
            self.calls.append(dict(payload))
        return self.result(payload)

    # 函数用途: 某个 claim 已续约的次数。
    def count(self, claim_id: str) -> int:
        with self.lock:
            return sum(1 for call in self.calls if call["claim_id"] == claim_id)


# 函数用途: 建一只心跳（不启动）。
def _heartbeat(claims: _Claims, claim_id: str) -> ConversationRunClaimHeartbeat:
    return ConversationRunClaimHeartbeat(
        {"store": SimpleNamespace(claims=claims), "thread_id": f"thread-{claim_id}", "claim_id": claim_id,
         "lease_seconds": 30, "interval_seconds": INTERVAL_SECONDS}
    )


# 函数用途: 等到条件成立或超时。
def _wait_until(predicate) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# 函数用途: 反复起停多批心跳，新建线程数有与回合数无关的上限（一条调度线程 + 有上限的续约池）。
def test_heartbeat_lifecycles_do_not_start_a_thread_each(monkeypatch):
    starts = []
    original_start = threading.Thread.start

    def counting_start(self):
        starts.append(self.name)
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", counting_start)
    claims = _Claims()
    for round_index in range(ROUNDS):
        batch = [_heartbeat(claims, f"r{round_index}-{index}") for index in range(HEARTBEATS)]
        for heartbeat in batch:
            heartbeat.start()
        try:
            assert _wait_until(lambda: all(claims.count(h.claim_id) >= RENEWS_BEFORE_STOP for h in batch))
        finally:
            for heartbeat in batch:
                heartbeat.stop()
        assert not any(heartbeat.is_alive() for heartbeat in batch)
    bound = 1 + min(32, (os.cpu_count() or 1) + 4)
    assert len(starts) <= bound < ROUNDS * HEARTBEATS, f"{ROUNDS * HEARTBEATS} 次心跳新建了 {len(starts)} 条线程"


# 函数用途: 续约按间隔反复进行且带原字段；stop 返回后不再续约，is_alive 变 False。
def test_renew_cadence_and_stop_fence():
    claims = _Claims()
    heartbeat = _heartbeat(claims, "cadence")
    heartbeat.start()
    try:
        assert _wait_until(lambda: claims.count("cadence") >= 3)
    finally:
        heartbeat.stop()
    renewed = claims.count("cadence")
    assert not heartbeat.is_alive()
    time.sleep(INTERVAL_SECONDS * 4)
    assert claims.count("cadence") == renewed, "stop 之后不得再续约"
    first = claims.calls[0]
    assert first["thread_id"] == "thread-cadence" and first["lease_seconds"] == 30 and "now" in first


# 函数用途: stop 等在途续约写完才返回（调用方随后 finish 租约，不能被迟到的续约覆盖）。
def test_stop_waits_for_the_in_flight_renew():
    entered, release = threading.Event(), threading.Event()

    def blocking(payload):
        entered.set()
        release.wait(WAIT_SECONDS)
        return payload

    heartbeat = _heartbeat(_Claims(blocking), "in-flight")
    heartbeat.start()
    assert entered.wait(WAIT_SECONDS)
    stopper = threading.Thread(target=heartbeat.stop, daemon=True)
    stopper.start()
    stopper.join(INTERVAL_SECONDS * 4)
    assert stopper.is_alive(), "续约未写完时 stop 不能返回"
    release.set()
    stopper.join(WAIT_SECONDS)
    assert not stopper.is_alive() and not heartbeat.is_alive()


# 函数用途: 续约返回 None 或抛错都让心跳自行结束，不经 excepthook 抛出原始异常。
def test_lost_or_failing_renew_ends_the_heartbeat_quietly(monkeypatch):
    hooked = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: hooked.append(args))
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.append(args))

    def failing(_payload):
        raise RuntimeError("renew failed")

    for name, claims in (("lost", _Claims(lambda _payload: None)), ("failing", _Claims(failing))):
        heartbeat = _heartbeat(claims, name)
        heartbeat.start()
        heartbeat.join(WAIT_SECONDS)
        assert not heartbeat.is_alive() and claims.count(name) == 1
        heartbeat.stop()
    assert hooked == []
