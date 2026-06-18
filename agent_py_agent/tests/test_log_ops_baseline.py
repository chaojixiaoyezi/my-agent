
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
