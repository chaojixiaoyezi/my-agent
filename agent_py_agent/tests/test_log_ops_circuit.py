"""源采集断路器单测(组件④):连续失败熔断 + 冷却跳过 + 成功复位。"""

from agent.tooling.log_ops.store import CollectMetrics, _SOURCE_FAIL_THRESHOLD, _SOURCE_COOLDOWN


def test_source_circuit_trips_on_consecutive_failures():
    m = CollectMetrics()
    sid = "src1"
    for i in range(_SOURCE_FAIL_THRESHOLD - 1):  # 阈值前不熔断
        assert m.note_source_health(sid, "api_request_failed: timeout", 1000.0 + i) is False
    tripped = m.note_source_health(sid, "api_request_failed: timeout", 1010.0)  # 第 threshold 次刚熔断
    assert tripped is True
    assert m.per_source[sid]["circuit"]["state"] == "open"  # 状态入 metrics,status 可见


def test_source_circuit_skips_during_cooldown():
    m = CollectMetrics()
    sid = "src1"
    for i in range(_SOURCE_FAIL_THRESHOLD):
        m.note_source_health(sid, "err", 1000.0 + i)
    breaker = m.breaker_for(sid)
    assert breaker.allow(now=breaker.opened_at + 10) is False  # 冷却内→daemon 跳过该源(省资源)
    assert breaker.allow(now=breaker.opened_at + _SOURCE_COOLDOWN + 1) is True  # 冷却到→half-open 试探一次


def test_source_circuit_recovers_on_success():
    m = CollectMetrics()
    sid = "src1"
    for i in range(_SOURCE_FAIL_THRESHOLD):
        m.note_source_health(sid, "err", 1000.0 + i)
    assert m.per_source[sid]["circuit"]["state"] == "open"
    m.breaker_for(sid).allow(now=1000.0 + _SOURCE_COOLDOWN + 1)  # → half_open
    m.note_source_health(sid, "", 1200.0)  # 一次成功采集(error 空)
    assert m.per_source[sid]["circuit"]["state"] == "closed"  # 复位
    assert m.breaker_for(sid).consecutive_failures == 0


def test_independent_breakers_per_source():
    m = CollectMetrics()
    for i in range(_SOURCE_FAIL_THRESHOLD):
        m.note_source_health("bad", "err", 1000.0 + i)
    m.note_source_health("good", "", 1000.0)
    assert m.per_source["bad"]["circuit"]["state"] == "open"
    assert m.per_source["good"]["circuit"]["state"] == "closed"  # 一个源熔断不影响别的源
