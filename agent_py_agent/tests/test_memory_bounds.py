"""审计 #16(part B/C,medium/稳定)真测:进程级数据结构无界增长 → 有界回收。

上市公司系统进程长驻数周不重启,这些结构随业务量(历史文件数/历史租户数/拒绝事件数)线性
膨胀必爆。这里真跑加载/限流/拒绝路径,断言活跃工作集之外的结构被回收、内存恒定:
- B  json_io 锁字典:WeakValueDictionary,锁无人持有即 GC;持锁期间绝不回收(互斥不破)。
- C1 限流器 buckets:水位到顶逐出已回满的惰性桶,在途限流中的桶保留(不放宽限流)。
- C2 worker_handler._REJECTED:deque(maxlen) 有界环,热路径每拒一条不再无界 append。
"""

from __future__ import annotations

import gc
from pathlib import Path

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.llm_scale import (
    ConcurrencyLimiter,
    LLMAdmission,
    TenantRateLimiter,
    TokenBudget,
)
from agent_py_agent.agent.llm_scale.rate_limiter import TokenBucket

# ---------- B: json_io 锁字典弱引用回收 ----------

def test_lock_dict_reclaims_idle_locks(tmp_path: Path) -> None:
    json_io._JSON_FILE_LOCKS.clear()
    gc.collect()
    # 真对 2000 个不同路径各做一次"取锁→进临界区→释放"(每个完整释放后才下一个)
    for i in range(2000):
        with json_io.locked_json_path(tmp_path / f"f{i}.json"):
            pass
    gc.collect()  # 无任何线程再持有这些 _PathLock → 弱字典应已全部摘除
    # 锁表只随"活跃文件数"而非"历史见过的文件数"增长:2000 个历史路径回收到近 0
    assert len(json_io._JSON_FILE_LOCKS) <= 5


def test_held_lock_is_same_object_and_not_reclaimed(tmp_path: Path) -> None:
    json_io._JSON_FILE_LOCKS.clear()
    p = tmp_path / "hot.json"
    key = str(p.resolve())
    with json_io.locked_json_path(p):
        # 持锁期间:同路径再取必须是同一把锁(否则两线程拿到不同锁 → 互斥失效)。
        # 用临时表达式比较身份,不绑名字,避免测试自己留下强引用妨碍回收断言。
        assert key in json_io._JSON_FILE_LOCKS  # 持锁期间强引用在,弱字典不回收
        assert json_io._path_lock(p) is json_io._path_lock(p)  # 同路径 → 同一对象
        gc.collect()
        assert key in json_io._JSON_FILE_LOCKS  # gc 也不摘(还在被持有)
    gc.collect()
    assert key not in json_io._JSON_FILE_LOCKS  # 释放后无任何强引用 → 回收


def test_held_lock_serializes_concurrent_threads(tmp_path: Path) -> None:
    import threading

    p = tmp_path / "race.json"
    inside = []
    overlap = []

    def worker() -> None:
        with json_io.locked_json_path(p):
            inside.append(1)
            if len(inside) > 1:
                overlap.append(1)  # 同时进临界区 = 互斥被破(回收把锁弄丢了)
            for _ in range(2000):
                pass  # 拉长持锁窗口,逼真竞争
            inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap == []  # 8 线程争同一路径全程互斥,弱回收没让锁失效


# ---------- C1: 限流器 buckets 水位回收 ----------

def test_rate_limiter_evicts_idle_full_buckets_at_water_level() -> None:
    clock = [0]
    rl = TenantRateLimiter(rps=1000, burst=1000, clock=lambda: clock[0], max_tenants=4)
    for i in range(4):
        assert rl.allow(f"t{i}", tokens=1)  # 各扣 1(999/1000,未满)
    assert rl.tenant_count() == 4
    clock[0] = 10_000  # 时钟前进 10s,rps=1000 → 4 个桶全部回满(idle)
    rl.allow("t-new", tokens=1)  # 第 5 个新租户:水位到顶 → 逐出已回满的 4 个 idle 桶
    assert rl.tenant_count() <= 4  # 不随历史租户数无界增长


def test_rate_limiter_keeps_throttled_bucket_state() -> None:
    clock = [0]
    rl = TenantRateLimiter(rps=0, burst=2, clock=lambda: clock[0], max_tenants=3)
    assert rl.allow("hot", tokens=2)  # hot 用尽令牌:2→0(rps=0 永不补)
    assert not rl.allow("hot", tokens=1)  # 确认在途限流中
    for i in range(3):
        rl.available(f"idle{i}")  # 建满桶制造水位压力,触发回收
    rl.available("trigger")
    # hot 不是"满"桶,绝不被逐出 → 仍 0 令牌(没被回收重置成满 = 没有限流绕过)
    assert not rl.allow("hot", tokens=1)


def test_token_bucket_is_full_after_refill() -> None:
    clock = [0]
    b = TokenBucket(capacity=5, refill_per_sec=5, clock=lambda: clock[0])
    assert b.try_consume(5)  # 5→0
    assert not b.is_full()
    clock[0] = 1000  # 1s → 回满
    assert b.is_full()


# ---------- C2: worker_handler._REJECTED 有界环 ----------

def _reject_all_admission() -> LLMAdmission:
    # 预算极小:任何 est 都超 → 全部 budget_exceeded 走拒绝 append 路径
    return LLMAdmission(TenantRateLimiter(1000, 1000), TokenBudget(1, 3600), ConcurrencyLimiter(8))


def test_rejected_buffer_is_bounded() -> None:
    from agent_py_agent.agent import worker_handler

    worker_handler.reset_for_test(_reject_all_admission())
    try:
        n = worker_handler._REJECTED_MAX + 500
        for i in range(n):
            worker_handler.handle({"tenant": f"t{i}", "estimated_tokens": 10_000})  # 真发被拒消息
        # 拒绝事件远超上限,但缓冲恒定在 maxlen(丢最旧留最近),不无界膨胀
        assert len(worker_handler._REJECTED) == worker_handler._REJECTED_MAX
        assert ("t" + str(n - 1), "budget_exceeded") in worker_handler._REJECTED  # 最近的还在
    finally:
        worker_handler.reset_for_test()
