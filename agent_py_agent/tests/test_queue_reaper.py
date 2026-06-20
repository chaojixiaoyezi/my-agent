"""审计 #5 修复真测:崩溃 worker 的租约回收 + 心跳抗瞬时 DB 错误。

真起队列/reaper 线程:worker 领了消息却不再心跳(模拟崩在多步 turn 中途)→ reaper 周期 recover_stale
把它退回 pending、整条 lane 解封(不再永久卡死);心跳调用每次抛错也不杀心跳线程(防 lease 过期→在途
消息被误回收重复处理)。学 claw/通道运行时 在 worker 循环里实际消费 reclaim_stale。
"""

from __future__ import annotations

import time

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
from agent_py_agent.agent.queue_worker import StaleReaper, _Heartbeat
from agent_py_agent.agent.storage_backend import StorageBackend


def _queue(tmp_path) -> IngressQueue:
    q = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"), QueueConfig(lane_cap=50))
    q.ensure_schema()
    return q


def test_recover_stale_reclaims_and_frees_lane(tmp_path) -> None:
    q = _queue(tmp_path)
    q.enqueue("e1", "laneX", {"n": 1})
    q.enqueue("e2", "laneX", {"n": 2})  # 同 lane
    assert q.claim(lease_seconds=0) is not None  # 领 e1,lease≈now,laneX 占用
    assert q.claim(lease_seconds=120) is None  # 同 lane 串行 → e2 暂不可领(worker 崩则永久卡这)
    time.sleep(0.02)  # 让当前时间越过 lease_until
    assert q.recover_stale() >= 1  # 过期租约退回 pending
    assert q.claim(lease_seconds=120) is not None  # lane 解封,可继续领


def test_reaper_thread_reclaims_crashed_worker_message(tmp_path) -> None:
    q = _queue(tmp_path)
    q.enqueue("e1", "L", {"n": 1})
    assert q.claim(lease_seconds=0) is not None  # worker 领了却不心跳(模拟崩溃)
    assert q.stats().get("claimed") == 1
    reaper = StaleReaper(q, interval=0.05)
    reaper.start()
    try:
        end = time.monotonic() + 3.0
        while q.stats().get("pending", 0) < 1 and time.monotonic() < end:
            time.sleep(0.05)
    finally:
        reaper.stop()
    assert q.stats().get("pending") == 1  # reaper 真把崩溃 worker 的消息回收成 pending
    assert q.stats().get("claimed", 0) == 0


class _FlakyQueue:
    def __init__(self) -> None:
        self.calls = 0

    def heartbeat(self, token: str, lease_seconds: int = 0) -> None:
        self.calls += 1
        raise RuntimeError("transient db blip")  # 每次心跳都炸


def test_heartbeat_survives_transient_db_error() -> None:
    q = _FlakyQueue()
    hb = _Heartbeat(q, "tok", 2)  # interval = max(1, 2/2) = 1s
    hb.start()
    try:
        time.sleep(2.2)  # 跑过约 2 个 interval
        assert q.calls >= 2  # 第一次心跳抛错后仍继续调 → 循环没被异常杀掉
        assert hb.is_alive()  # 心跳线程仍活(否则 lease 过期,在途消息被误回收重复处理)
    finally:
        hb.stop()
