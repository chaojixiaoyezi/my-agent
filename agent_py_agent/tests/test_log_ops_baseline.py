
from __future__ import annotations

"""统计基线 + 数据驱动异常检测单测 —— 初筛从死正则升级到"偏离正常"(UEBA 最小内核)。"""

from agent_py_agent.agent.tooling.log_ops import baseline


def test_extract_entities() -> None:
    ents = baseline.extract_entities('ts=2026 user=alice src_ip=45.1.2.3 status=200')
    assert ents["user"] == "alice"
    assert ents["ip"] == "45.1.2.3"  # 首个 IP 归 ip 字段
    assert ents["status"] == "200"


def test_new_entity_anomaly() -> None:
    lines = ["user=alice action=read"] * 60 + ["user=bob action=read"] * 60
    b = baseline.build_baseline(lines)
    s0, _ = baseline.score_anomaly(b, "user=alice action=read", min_records=100)
    assert s0 == 0.0  # 已知 user 正常
    s1, r1 = baseline.score_anomaly(b, "user=mallory action=read", min_records=100)
    assert s1 > 0 and any("新实体" in x for x in r1)  # 没见过的 user → 异常


def test_high_cardinality_field_skipped() -> None:
    lines = [f"ts=t{i} user=alice" for i in range(60)]  # ts 每条不同=高基数
    b = baseline.build_baseline(lines)
    s, _ = baseline.score_anomaly(b, "ts=t9999 user=alice", min_records=50)
    assert s == 0.0  # 新 ts 不算异常(高基数跳过), user 已知 → 无异常


def test_high_cardinality_via_cap_overflow() -> None:
    """字段值种类超过 cap(时间戳/id)即使 distinct/total 被大 total 稀释成假低基数,也判高基数跳过(修 cap bug,
    否则每个新值都误报,实测把候选撑到 200 万)。"""
    lines = [f"sid=s{i} user=alice" for i in range(300)] + ["sid=s0 user=alice"] * 5000
    b = baseline.build_baseline(lines)
    assert b.fields["sid"].is_high_card()  # 300>cap256 种值 → 高基数
    s, _ = baseline.score_anomaly(b, "sid=sNEW user=alice", min_records=100)
    assert s == 0.0  # 新 sid 不误报(高基数跳过)


def test_extract_strips_timestamp_and_json() -> None:
    web = baseline.extract_entities('10.0.5.1 - - [2026-06-18T11:23:45+00:00] "GET /x HTTP/1.1" 200')
    assert "t11" not in web and web.get("ip") == "10.0.5.1"  # 时间戳碎片不入字段
    js = baseline.extract_entities('{"ts":"2026-06-18T11:23:45+00:00","user":"alice","result":"ok"}')
    assert js.get("user") == "alice" and js.get("result") == "ok"  # JSON "key":"value" 提取


def test_cold_start_no_judgment() -> None:
    b = baseline.build_baseline(["user=alice"] * 10)
    s, r = baseline.score_anomaly(b, "user=mallory", min_records=100)
    assert s == 0.0 and r == []  # 样本不足 → 只学不判,不把什么都当新


def test_rare_value_anomaly() -> None:
    lines = ["user=alice"] * 199 + ["user=root"]  # root 占比 0.5% < 1% 阈值
    b = baseline.build_baseline(lines)
    s, r = baseline.score_anomaly(b, "user=root", min_records=100)
    assert s > 0 and any("罕见" in x for x in r)


def test_baseline_roundtrip() -> None:
    b = baseline.build_baseline(["user=alice ip=10.0.0.1"] * 50)
    b2 = baseline.SourceBaseline.from_dict(b.to_dict())
    assert b2.records == 50 and b2.fields["user"].values["alice"] == 50


def test_numeric_measure_field_skipped() -> None:
    """纯数值度量字段(ms/rows/port 延迟计数)不做实体检测——每个值本就不同,新值/罕见值无安全意义。"""
    lines = [f"path=/api ms={i % 200} status=200" for i in range(300)]
    b = baseline.build_baseline(lines)
    assert baseline.score_anomaly(b, "path=/api ms=999 status=200", min_records=100)[0] == 0.0  # 新 ms 值(数值)不报
    s2, r2 = baseline.score_anomaly(b, "path=/etc/shadow ms=50 status=200", min_records=100)
    assert s2 > 0 and any("path" in x for x in r2)  # 新 path(类别)仍报
