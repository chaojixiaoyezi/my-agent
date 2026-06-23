
from __future__ import annotations

"""③ML 推理引擎:SignalCluster → 三路融合评分 → band/route → MLSignalAssessment → MLAnalysisResult。

三路(设计 §4.2):
- 监督路 _supervised_risk:severity/count/突发/置信/统计异常确定性加权(first-pass)。**M3 换训练
  分类器只需重写这一个函数**,上下游契约不动(claw 留的口子)。
- 无监督路:复用 baseline 统计异常分(抓未知/0-day,M1 直接取簇的 statistical_anomaly)。
- 关联路:M1 占位 0,M3 接图关联。
融合权重 weights 可配可学(feedback 回流调)。防爆:评估按 fused_score 降序截断到 limit,临时
worker 数封顶(非 LOW 域数与 max_workers 取小),省略数记 omitted_assessment_count 不静默丢。
"""

import math
from collections import Counter, defaultdict

from . import explain
from .models import (
    AnomalyBand,
    CoordinationRoute,
    MLAnalysisResult,
    MLCoordinationPlan,
    MLFeatureVector,
    MLScoreBreakdown,
    MLSignalAssessment,
    SignalCluster,
    SignalOutcome,
)

DEFAULT_FUSION_WEIGHTS = {"supervised": 0.5, "unsupervised": 0.3, "correlation": 0.2}
_MAX_ASSESSMENTS = 200
_MAX_TEMP_WORKERS = 16

_ROUTE_BY_BAND = {
    AnomalyBand.CRITICAL: CoordinationRoute.MAIN_AGENT_ESCALATION,
    AnomalyBand.HIGH: CoordinationRoute.CHILD_DOMAIN_REVIEW,
    AnomalyBand.MEDIUM: CoordinationRoute.TEMP_GRANDCHILD_EVIDENCE_SCAN,
    AnomalyBand.LOW: CoordinationRoute.TEMP_GRANDCHILD_EVIDENCE_SCAN,
}


def build_feature(cluster: SignalCluster) -> MLFeatureVector:
    """SignalCluster → 特征向量(③推理统一输入契约)。"""
    return MLFeatureVector(
        cluster_id=cluster.cluster_id,
        domain_id=cluster.domain_id,
        fingerprint=cluster.fingerprint,
        alert_type=cluster.fingerprint,
        severity=cluster.severity,
        count=cluster.count,
        confidence=cluster.confidence,
        duration_seconds=cluster.duration_seconds,
        count_per_minute=cluster.count_per_minute,
        cardinality=cluster.distinct_targets,
        fan_out=cluster.fan_out,
        rate_deviation=0.0,
        statistical_anomaly=cluster.statistical_anomaly,
        correlation_boost=cluster.correlation_boost,
    )


def _supervised_risk(feature: MLFeatureVector) -> float:
    """监督路确定性加权(设计 §4.2)。M3 换训练分类器即替换此函数,契约不变。"""
    sev = min(1.0, feature.severity / 5)
    cnt = min(1.0, math.log1p(feature.count) / math.log1p(1000))
    burst = min(1.0, feature.count_per_minute / 50)
    return round(
        sev * 0.40 + cnt * 0.20 + burst * 0.20 + feature.confidence * 0.10 + feature.statistical_anomaly * 0.10,
        4,
    )


def _unsupervised_anomaly(feature: MLFeatureVector) -> float:
    """无监督路:统计异常 + UEBA 扇出(一个实体打多个目标=扫描行为)取大者。抓规则看不见的行为异常。
    扇出 20+ 视为强扫描信号(归一到 1)。指纹簇 fan_out=0 时退化为纯统计异常。"""
    stat = min(1.0, feature.statistical_anomaly)
    fanout_signal = min(1.0, feature.fan_out / 20) if feature.fan_out else 0.0
    return round(max(stat, fanout_signal), 4)


def supervised_baseline(feature: MLFeatureVector) -> float:
    """确定性监督 baseline(影子对比 M3-5 用,= 现确定性加权)。LR 达标后由 model.predict 替换。"""
    return _supervised_risk(feature)


def score_feature(feature: MLFeatureVector, weights: dict[str, float]) -> MLScoreBreakdown:
    """三路融合评分。无监督=统计异常+UEBA 扇出,关联=M1 占位 0。"""
    supervised = _supervised_risk(feature)
    unsupervised = _unsupervised_anomaly(feature)
    correlation = min(1.0, feature.correlation_boost)  # M3-3:簇实体在攻击链上则抬高(关联路真做)
    fused = round(
        weights.get("supervised", 0.5) * supervised
        + weights.get("unsupervised", 0.3) * unsupervised
        + weights.get("correlation", 0.2) * correlation,
        4,
    )
    return MLScoreBreakdown(supervised, unsupervised, correlation, fused)


def band_for(score: float, cluster: SignalCluster) -> AnomalyBand:
    """评分 + 硬规则 → 分级(claw 阈值:高危规则即便分不够也升级,召回优先)。"""
    if score >= 0.78 or (cluster.severity >= 5 and cluster.count >= 20):
        return AnomalyBand.CRITICAL
    if score >= 0.58 or cluster.severity >= 3:
        return AnomalyBand.HIGH
    if score >= 0.30:
        return AnomalyBand.MEDIUM
    return AnomalyBand.LOW


def route_for(band: AnomalyBand) -> CoordinationRoute:
    return _ROUTE_BY_BAND[band]


def predict_outcome(feature: MLFeatureVector, breakdown: MLScoreBreakdown) -> tuple[SignalOutcome, float]:
    """M1 无标注,按分数粗猜 outcome + 低置信(故 uncertainty 高);feedback 有标注后才转准。"""
    if breakdown.fused_score >= 0.78:
        return SignalOutcome.ATTEMPT, 0.4
    return SignalOutcome.OTHER, 0.2


def assess_cluster(cluster: SignalCluster, weights: dict[str, float]) -> MLSignalAssessment:
    """单簇评级:组装三路评分 + band + route + 解释层。"""
    feature = build_feature(cluster)
    breakdown = score_feature(feature, weights)
    band = band_for(breakdown.fused_score, cluster)
    route = route_for(band)
    outcome, outcome_conf = predict_outcome(feature, breakdown)
    return MLSignalAssessment(
        cluster_id=cluster.cluster_id,
        domain_id=cluster.domain_id,
        fused_score=breakdown.fused_score,
        band=band,
        route=route,
        breakdown=breakdown,
        predicted_outcome=outcome,
        outcome_confidence=outcome_conf,
        feature=feature,
        narrative=explain.narrate(cluster, feature, band),
        uncertainty=explain.uncertainty_for(cluster, breakdown),
        supporting_context=explain.context_for(cluster),
        evidence_refs=cluster.evidence_refs,
        reasons=explain.reasons_for(cluster, breakdown),
        ai_hint=explain.ai_hint_for(band, route),
    )


def _build_plan(assessments: list[MLSignalAssessment], max_workers: int) -> MLCoordinationPlan:
    """协调计划:统计 route/band 分布 + 该派几个临时 worker(非 LOW 域数与 max_workers 取小)。"""
    route_counts: Counter[str] = Counter(a.route.value for a in assessments)
    band_counts: Counter[str] = Counter(a.band.value for a in assessments)
    domain_clusters: dict[str, list[str]] = defaultdict(list)
    for assessment in assessments:
        domain_clusters[assessment.domain_id].append(assessment.cluster_id)
    non_low_domains = {a.domain_id for a in assessments if a.band != AnomalyBand.LOW}
    workers = min(max_workers, len(non_low_domains))
    note = f"{band_counts.get('critical', 0)} critical / {band_counts.get('high', 0)} high 待研判"
    return MLCoordinationPlan(
        temporary_worker_count=workers,
        domain_clusters=dict(domain_clusters),
        route_counts=dict(route_counts),
        band_counts=dict(band_counts),
        main_agent_note=note,
    )


def analyze(clusters: list[SignalCluster], weights: dict[str, float] | None = None, limit: int = _MAX_ASSESSMENTS, max_workers: int = _MAX_TEMP_WORKERS) -> MLAnalysisResult:
    """簇列表 → 评级 + 降序 + 防爆截断 + 协调计划。log_ml_analyze 的核心入口。"""
    active_weights = weights or DEFAULT_FUSION_WEIGHTS
    assessed = sorted(
        (assess_cluster(c, active_weights) for c in clusters),
        key=lambda a: a.fused_score,
        reverse=True,
    )
    kept = assessed[:limit]
    omitted = max(0, len(assessed) - len(kept))
    total_candidates = sum(c.count for c in clusters if c.cluster_kind == "fingerprint")
    return MLAnalysisResult(
        total_candidates=total_candidates,
        shard_count=len({c.domain_id for c in clusters}),
        total_cluster_count=len(clusters),
        assessment_limit=limit,
        omitted_assessment_count=omitted,
        assessments=tuple(kept),
        coordination_plan=_build_plan(kept, max_workers),
    )


__all__ = [
    "DEFAULT_FUSION_WEIGHTS",
    "build_feature",
    "score_feature",
    "band_for",
    "route_for",
    "predict_outcome",
    "assess_cluster",
    "analyze",
]
