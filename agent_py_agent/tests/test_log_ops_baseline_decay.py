"""基线衰减单测:LRU 容量淘汰 + TTL 衰减 + 攻击污染可恢复 + 兼容旧基线。"""

from agent.tooling.log_ops.baseline import (
    SourceBaseline,
    _FieldStat,
    _VALUES_CAP,
    score_anomaly,
)


def test_lru_evicts_least_recently_seen_on_cap():
    fs = _FieldStat()
    for i in range(_VALUES_CAP):
        fs.observe(f"v{i}", seq=i)  # v0 最久未见
    assert len(fs.values) == _VALUES_CAP
    fs.observe("vNEW", seq=_VALUES_CAP)  # 满 cap,加新值
    assert len(fs.values) == _VALUES_CAP  # 仍 = cap(淘汰一个)
    assert "vNEW" in fs.values
    assert "v0" not in fs.values  # 最久未见被淘汰,新值进得来


def test_decay_evicts_stale_values():
    fs = _FieldStat()
    fs.observe("old_ip", seq=10)
    fs.observe("recent_ip", seq=10_000)
    removed = fs.decay(now_seq=10_000, window=1_000)  # old_ip 落后 9990 > 1000 → 淘汰
    assert removed == 1
    assert "old_ip" not in fs.values
    assert "recent_ip" in fs.values  # 近期的留下


def test_attack_pollution_recovers_after_decay():
    """攻击IP被误学进基线→暂时被当"已知";攻击停止后 decay 清除它,再现重新算新实体(污染可恢复),
    而持续活跃的正常IP(last_seen 不断刷新)不被误淘汰。"""
    b = SourceBaseline()
    for i in range(150):  # 学够 min_records,基线成形
        b.observe_line(f"2026-06-18 user login from 10.0.0.{i % 3}")
    for _ in range(20):  # 攻击IP早期被多次误学成"已知正常"(低 seq=1,之后不再出现)
        b.fields["ip"].observe("9.9.9.9", seq=1)
    assert "9.9.9.9" in b.fields["ip"].values  # 污染:攻击IP混进基线
    b.records = 200_000  # 时间推进远超衰减窗口
    for _ in range(5):  # 正常IP持续活跃,last_seen 刷新到当下
        b.fields["ip"].observe("10.0.0.0", seq=200_000)
    pruned = b.decay(window=100_000)
    assert pruned >= 1
    assert "9.9.9.9" not in b.fields["ip"].values  # 停止出现的攻击IP被淘汰(污染清除)
    assert "10.0.0.0" in b.fields["ip"].values  # 持续活跃的正常IP留下(不误淘汰)
    s2, r2 = score_anomaly(b, "2026-06-18 evt login from 9.9.9.9")
    assert any("9.9.9.9" in x for x in r2)  # 攻击IP再现重新告警


def test_legacy_baseline_without_last_seen_does_not_crash():
    """旧基线(无 last_seen 字段)加载后,满 cap 再 observe 走 LRU 不崩,老值优先淘汰。"""
    data = {
        "records": 200,
        "fields": {"ip": {"values": {f"10.0.0.{i}": 5 for i in range(_VALUES_CAP)}, "total": 1000}},
    }
    b = SourceBaseline.from_dict(data)
    assert b.fields["ip"].last_seen == {}  # 旧档无 last_seen
    b.observe_line("2026-06-18 evt login from 1.2.3.4")  # 满 cap,触发 LRU
    assert "1.2.3.4" in b.fields["ip"].values
    assert len(b.fields["ip"].values) == _VALUES_CAP  # 淘汰一个老值,容量守住


def test_roundtrip_preserves_last_seen():
    b = SourceBaseline()
    for i in range(50):
        b.observe_line(f"2026-06-18 evt login from 10.0.0.{i % 4}")
    b2 = SourceBaseline.from_dict(b.to_dict())
    assert b2.fields["ip"].last_seen == b.fields["ip"].last_seen
    assert b2.fields["ip"].values == b.fields["ip"].values
