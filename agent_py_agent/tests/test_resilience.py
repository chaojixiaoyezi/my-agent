"""通用韧性原语单测:断路器 + 滑动窗口速率检测。"""

from agent.common.resilience import BurstTracker, CircuitBreaker, RateWindow


# ---- CircuitBreaker ----
def test_circuit_breaker_trips_exactly_at_threshold():
    cb = CircuitBreaker(threshold=3, cooldown_seconds=60)
    assert cb.on_failure(now=0) is False
    assert cb.on_failure(now=1) is False
    assert cb.on_failure(now=2) is True  # 第3次刚好熔断,返回 True 供"刚熔断"告警一次
    assert cb.state == "open"
    assert cb.on_failure(now=3) is False  # 已 open,不重复返回 True(不重复告警)


def test_circuit_breaker_open_blocks_until_cooldown():
    cb = CircuitBreaker(threshold=2, cooldown_seconds=60)
    cb.on_failure(now=0)
    cb.on_failure(now=0)
    assert cb.state == "open"
    assert cb.allow(now=30) is False  # 冷却未到→快速失败
    assert cb.allow(now=60) is True   # 冷却到→转 half_open 放一次试探
    assert cb.state == "half_open"


def test_circuit_breaker_success_closes_and_resets():
    cb = CircuitBreaker(threshold=2)
    cb.on_failure(now=0)
    cb.on_failure(now=0)
    cb.allow(now=100)  # → half_open
    cb.on_success()
    assert cb.state == "closed"
    assert cb.consecutive_failures == 0


def test_circuit_breaker_success_resets_consecutive_not_total():
    cb = CircuitBreaker(threshold=3)
    cb.on_failure(now=0)
    cb.on_failure(now=1)
    cb.on_success()  # 连续清零,累计保留
    assert cb.consecutive_failures == 0
    assert cb.on_failure(now=2) is False  # 重新计数,不熔断
    assert cb.on_failure(now=3) is False
    assert cb.state == "closed"
    assert cb.total_failures == 4  # 累计不清零


def test_circuit_breaker_snapshot_serializable():
    cb = CircuitBreaker(threshold=1)
    cb.on_failure(now=5)
    snap = cb.snapshot()
    assert snap["state"] == "open"
    assert snap["total_failures"] == 1
    assert snap["opened_at"] == 5


def test_circuit_breaker_gradual_recovery_needs_consecutive_successes():
    """[R2]success_threshold=2:half_open 需连续2次成功才完全 closed(防恢复抖动)。"""
    cb = CircuitBreaker(threshold=2, cooldown_seconds=60, success_threshold=2)
    cb.on_failure(now=0)
    cb.on_failure(now=0)
    cb.allow(now=100)  # → half_open
    cb.on_success()  # 第1次成功,未达阈值
    assert cb.state == "half_open"
    cb.on_success()  # 第2次成功
    assert cb.state == "closed"  # 连续2次才恢复


def test_circuit_breaker_half_open_failure_reopens_and_resets():
    """[R2]half_open 中途失败→清零成功计数 + 重新 open(恢复需重新连续成功)。"""
    cb = CircuitBreaker(threshold=2, cooldown_seconds=60, success_threshold=2)
    cb.on_failure(now=0)
    cb.on_failure(now=0)
    cb.allow(now=100)  # half_open
    cb.on_success()
    assert cb.half_open_successes == 1
    cb.on_failure(now=101)  # 失败→重新 open
    assert cb.state == "open"
    assert cb.half_open_successes == 0


# ---- RateWindow ----
def test_rate_window_counts_within_window():
    rw = RateWindow(window_seconds=10)
    assert [rw.observe(now=t) for t in [0, 1, 2]] == [1, 2, 3]


def test_rate_window_evicts_expired():
    rw = RateWindow(window_seconds=10)
    rw.observe(now=0)
    rw.observe(now=1)
    assert rw.count(now=20) == 0  # 全部滑出窗口


def test_rate_window_burst_detection():
    rw = RateWindow(window_seconds=10)
    for t in range(5):
        rw.observe(now=t)
    assert rw.is_burst(now=5, limit=5) is True   # 窗口内 5 次达阈值
    assert rw.is_burst(now=5, limit=6) is False  # 未达
    assert rw.is_burst(now=100, limit=1) is False  # 时间推进全淘汰,不再突发


def test_rate_window_sliding_partial_eviction():
    rw = RateWindow(window_seconds=10)
    rw.observe(now=0)
    rw.observe(now=5)
    rw.observe(now=9)
    assert rw.count(now=11) == 2  # now-10=1,只淘汰 now=0 那条,留 5 和 9


# ---- BurstTracker ----
def test_burst_tracker_detects_high_frequency_entity():
    bt = BurstTracker(window=100, threshold=5)
    results = [bt.observe("9.9.9.9") for _ in range(5)]
    assert results[0] is False
    assert results[-1] is True  # 第5次达阈值=突发(暴力破解/扫描特征)


def test_burst_tracker_independent_entities():
    bt = BurstTracker(window=100, threshold=3)
    res = [bt.observe(e) for e in ["a", "b", "a", "b", "a"]]  # a 出现3次
    assert res[4] is True  # a 第3次达阈值,各实体独立计数


def test_burst_tracker_window_slides():
    bt = BurstTracker(window=5, threshold=3)
    bt.observe("a")  # seq1
    for _ in range(10):
        bt.observe("other")  # 把 a 的 seq1 滑出窗口
    assert bt.observe("a") is False  # a 在窗口内只剩这次,不突发(低频不误报)


def test_burst_tracker_lru_capacity():
    bt = BurstTracker(window=1000, threshold=2, capacity=2)
    bt.observe("a")
    bt.observe("b")
    bt.observe("c")  # 超容量,LRU 淘汰最久未用的 a
    assert bt.observe("a") is False  # a 被淘汰后重新计数,不会因旧账突发
