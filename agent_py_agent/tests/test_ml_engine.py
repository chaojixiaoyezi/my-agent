
from __future__ import annotations

"""ML 引擎(ml_engine)骨架单测 —— M1 task#71。

钉死:reduce 按(源:规则)聚类压缩、engine 三路评分单调、band 分级阈值+硬规则、analyze 降序截断+
omitted 记数、to_dict 可 json 序列化(Enum 是 str 子类)、log_ml_analyze 工具读候选出评级端到端。
"""

import json
from pathlib import Path

from agent_py_agent.agent.ml_engine import engine, feedback, reducer
from agent_py_agent.agent.ml_engine.models import (
    AnomalyBand,
    CoordinationRoute,
    OutcomeLabel,
    SignalCluster,
    SignalOutcome,
)
from agent_py_agent.agent.tooling.log_ops.store import SourceSpec
from agent_py_agent.agent.tooling.log_ops.tools import _store
from agent_py_agent.agent.tooling.log_ops.tools_ml import LogMlAnalyzeTool, LogMlLabelTool
from agent_py_agent.agent.tooling.log_ops.triage import TriageInput, triage_line


def _cand(source_id: str, rules: list[str], severity: str, *, line: int = 1, anomaly: float = 0.0, ts: float = 1000.0) -> dict:
    cand = {
        "fingerprint": f"cand-{source_id}-{line}",
        "source_id": source_id,
        "source_kind": "api",
        "source_locator": f"http://{source_id}",
        "line_no": line,
        "raw_line": f"line {line} {' '.join(rules)}",
        "matched_rules": rules,
        "severity": severity,
        "alert_id": "",
        "timestamp": "",
        "detected_at": ts,
    }
    if anomaly > 0:
        cand["anomaly_score"] = anomaly
        cand["anomaly_reasons"] = ["test"]
    return cand


def _cluster(severity: int, count: int, *, conf: float = 0.6, first: float = 0.0, last: float = 1.0, anomaly: float = 0.0, cid: str = "d:f", kind: str = "fingerprint", fan_out: int = 0) -> SignalCluster:
    return SignalCluster(cid, "d", "f", kind, severity, count, conf, first, last, distinct_targets=fan_out, fan_out=fan_out, statistical_anomaly=anomaly)


def test_reduce_clusters_by_source_and_rules() -> None:
    """同(源:规则)聚一簇,不同的分开;压缩比 = 候选数/簇数。"""
    cands = [_cand("api1", ["sql_injection"], "high", line=i) for i in range(50)]
    cands += [_cand("api1", ["xss"], "medium", line=i) for i in range(50, 80)]
    cands += [_cand("api2", ["sql_injection"], "high", line=i) for i in range(80, 90)]
    clusters = reducer.reduce_candidates(cands)
    assert len(clusters) == 3  # (api1:sql)(api1:xss)(api2:sql)
    by_id = {c.cluster_id: c for c in clusters}
    assert by_id["api1:sql_injection"].count == 50
    assert by_id["api1:sql_injection"].severity == 5  # high → 5
    assert by_id["api1:xss"].severity == 3  # medium → 3
    assert len(cands) / len(clusters) == 30  # 90 候选压成 3 簇,30× 压缩


def test_reduce_empty_and_bad_items() -> None:
    assert reducer.reduce_candidates([]) == []
    assert reducer.reduce_candidates([None, "bad", 123]) == []  # 坏项跳过不崩


def test_supervised_score_monotonic() -> None:
    """severity/count/突发越高,监督分与融合分越高。"""
    low = engine.score_feature(engine.build_feature(_cluster(1, 1, conf=0.5, last=0.0)), engine.DEFAULT_FUSION_WEIGHTS)
    high = engine.score_feature(engine.build_feature(_cluster(5, 500, conf=1.0, last=60.0)), engine.DEFAULT_FUSION_WEIGHTS)
    assert high.supervised_risk > low.supervised_risk
    assert high.fused_score > low.fused_score


def test_unsupervised_path_lifts_fused() -> None:
    """统计异常(无监督路)抬高融合分:同簇有 anomaly 比无 anomaly 分高。"""
    plain = engine.score_feature(engine.build_feature(_cluster(2, 5)), engine.DEFAULT_FUSION_WEIGHTS)
    anom = engine.score_feature(engine.build_feature(_cluster(2, 5, anomaly=0.9)), engine.DEFAULT_FUSION_WEIGHTS)
    assert anom.unsupervised_anomaly > 0
    assert anom.fused_score > plain.fused_score


def test_band_thresholds_and_hard_rules() -> None:
    """分级阈值 + 硬规则(high 规则即便分不够也升级,召回优先)。"""
    big = _cluster(5, 30)  # sev5 count30
    assert engine.band_for(0.9, big) == AnomalyBand.CRITICAL
    assert engine.band_for(0.1, big) == AnomalyBand.CRITICAL  # sev5&count>=20 硬规则
    mid = _cluster(3, 5)
    assert engine.band_for(0.1, mid) == AnomalyBand.HIGH  # sev3 硬规则 → HIGH
    lowc = _cluster(1, 1)
    assert engine.band_for(0.35, lowc) == AnomalyBand.MEDIUM
    assert engine.band_for(0.1, lowc) == AnomalyBand.LOW


def test_route_mapping() -> None:
    assert engine.route_for(AnomalyBand.CRITICAL) == CoordinationRoute.MAIN_AGENT_ESCALATION
    assert engine.route_for(AnomalyBand.HIGH) == CoordinationRoute.CHILD_DOMAIN_REVIEW
    assert engine.route_for(AnomalyBand.LOW) == CoordinationRoute.TEMP_GRANDCHILD_EVIDENCE_SCAN


def test_analyze_sorts_and_caps() -> None:
    """评级按 fused_score 降序 + 截断到 limit + omitted 记数。"""
    clusters = [_cluster((i % 5) + 1, i + 1, cid=f"d:{i}") for i in range(10)]
    result = engine.analyze(clusters, limit=3)
    assert len(result.assessments) == 3
    assert result.omitted_assessment_count == 7
    scores = [a.fused_score for a in result.assessments]
    assert scores == sorted(scores, reverse=True)
    assert result.total_cluster_count == 10


def test_analysis_result_json_serializable() -> None:
    """to_dict 可 json.dumps(Enum 是 str 子类,直接出字符串值,工具层不必特判)。"""
    result = engine.analyze([_cluster(5, 30)])
    parsed = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert parsed["assessments"][0]["band"] == "critical"
    assert parsed["assessments"][0]["route"] == "main_agent_escalation"
    assert "narrative" in parsed["assessments"][0]
    assert "uncertainty" in parsed["assessments"][0]


def test_log_ml_analyze_tool_end_to_end(tmp_path: Path) -> None:
    """工具读候选队列 → 降维评级 → 出 MLAnalysisResult。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    store.append_candidates([_cand("api1", ["sql_injection"], "high", line=i, anomaly=0.8) for i in range(25)])
    result = LogMlAnalyzeTool(tmp_path).execute({"monitor_id": "default"})
    assert result.ok
    payload = result.result_envelope
    assert payload["ok"] is True
    assert payload["read_candidates"] == 25
    assert payload["total_cluster_count"] == 1
    assert payload["assessments"][0]["band"] in ("critical", "high")


def test_triage_candidate_carries_entities() -> None:
    """UEBA 地基:candidate 带 extract_entities 解析的实体字段(以前提取了却丢,下游拿不到)。"""
    src = SourceSpec(kind="api", locator="http://api1", source_id="api1")
    cand = triage_line(src, TriageInput(line_no=1, raw_line="sql injection from 192.168.1.5 user=admin dst=10.0.0.9"))
    assert cand is not None
    assert cand["entities"].get("ip") == "192.168.1.5"
    assert cand["entities"].get("user") == "admin"


def test_reduce_by_entity_counts_fanout() -> None:
    """实体簇:同一 IP 打多个不同目标 → 扇出 = 不同目标数(抓横向扫描;指纹簇里会被打散)。"""
    cands = []
    for i in range(10):
        cand = _cand("api1", ["port_scan"], "medium", line=i)
        cand["entities"] = {"ip": "10.0.0.1", "dst": f"192.168.1.{i}"}
        cands.append(cand)
    clusters = reducer.reduce_by_entity(cands)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "entity"
    assert clusters[0].fan_out == 10
    assert clusters[0].distinct_targets == 10


def test_reduce_by_entity_skips_candidate_without_ip() -> None:
    cand = _cand("api1", ["xss"], "medium")
    cand["entities"] = {}
    assert reducer.reduce_by_entity([cand]) == []


def test_fanout_lifts_unsupervised_and_fused_score() -> None:
    """高扇出实体簇(扫描)比无扇出同条件簇评分高 —— UEBA 信号挂无监督路。"""
    base = _cluster(2, 10)
    scan = _cluster(2, 10, kind="entity", fan_out=30, cid="api1:entity:10.0.0.1")
    s_base = engine.score_feature(engine.build_feature(base), engine.DEFAULT_FUSION_WEIGHTS)
    s_scan = engine.score_feature(engine.build_feature(scan), engine.DEFAULT_FUSION_WEIGHTS)
    assert s_scan.unsupervised_anomaly > s_base.unsupervised_anomaly
    assert s_scan.fused_score > s_base.fused_score


def test_feedback_label_roundtrip_and_owner_isolation(tmp_path: Path) -> None:
    """标注写盘到 store_root + 读回;不同 owner(不同 root)互不可见(私有隔离)。"""
    root_a, root_b = tmp_path / "ownerA", tmp_path / "ownerB"
    root_a.mkdir()
    root_b.mkdir()
    feedback.append_label(root_a, OutcomeLabel("c1", SignalOutcome.SUCCESS, None, "r", "agent", 1.0))
    assert len(feedback.read_labels(root_a)) == 1
    assert feedback.read_labels(root_b) == []  # owner 隔离:B 看不到 A 的标注


def test_learn_weights_shifts_on_label_outcome() -> None:
    """真威胁标注(success/attempt)多 → 无监督权重升;误报(other)多 → 监督权重升;始终归一化。"""
    base = engine.DEFAULT_FUSION_WEIGHTS
    threats = [OutcomeLabel(f"c{i}", SignalOutcome.SUCCESS, None, "", "a", 1.0) for i in range(5)]
    benign = [OutcomeLabel(f"c{i}", SignalOutcome.OTHER, None, "", "a", 1.0) for i in range(5)]
    w_threat = feedback.learn_weights(base, threats)
    w_benign = feedback.learn_weights(base, benign)
    assert w_threat["unsupervised"] > base["unsupervised"]
    assert w_benign["supervised"] > base["supervised"]
    assert abs(sum(w_threat.values()) - 1.0) < 1e-6


def test_label_changes_next_analyze_weights(tmp_path: Path) -> None:
    """标注回流后下次 log_ml_analyze 的融合权重确实变了 —— 会学闭环 M1 落地。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    store.append_candidates([_cand("api1", ["sql_injection"], "high", line=i) for i in range(5)])
    before = LogMlAnalyzeTool(tmp_path).execute({}).result_envelope["fusion_weights"]
    for _ in range(3):
        LogMlLabelTool(tmp_path).execute({"cluster_id": "api1:sql_injection", "outcome": "attempt"})
    after = LogMlAnalyzeTool(tmp_path).execute({}).result_envelope["fusion_weights"]
    assert after["unsupervised"] > before["unsupervised"]  # 真威胁标注让无监督权重升
