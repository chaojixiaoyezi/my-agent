
from __future__ import annotations

"""解释层:把评级翻成人话(narrative)+ 标不确定度(uncertainty)+ 三路原因/派工提示。

业界教训(2026 agentic SOC):别只给裸分数——会导致系统性过度/不足信任。每个评级带一句话叙述 +
不确定度 + 可审原因,让 agent 判断该不该信这条建议。M1 没有训练标注,uncertainty 先天偏高,
主动促 agent 复核而非盲信确定性分数。
"""

from .models import (
    AnomalyBand,
    CoordinationRoute,
    MLFeatureVector,
    MLScoreBreakdown,
    SignalCluster,
)

_ROUTE_HINT = {
    CoordinationRoute.MAIN_AGENT_ESCALATION: "升级主代理立即研判(critical)",
    CoordinationRoute.CHILD_DOMAIN_REVIEW: "派子代理做域审查(high)",
    CoordinationRoute.TEMP_GRANDCHILD_EVIDENCE_SCAN: "临时孙代理扫证据即可(medium/low)",
}


def narrate(cluster: SignalCluster, feature: MLFeatureVector, band: AnomalyBand) -> str:
    """一句话叙述这个簇(源/条数/严重度/时长/扇出/突发 → band)。"""
    dur = int(cluster.duration_seconds)
    base = f"源 {cluster.domain_id} 出现 {cluster.count} 条「{cluster.fingerprint}」类告警(severity {cluster.severity}/5,{dur}s 内)"
    if cluster.cluster_kind == "entity" and cluster.fan_out > 0:
        base += f",单实体扇出 {cluster.fan_out} 个目标"
    if feature.count_per_minute >= 50:
        base += f",突发 {feature.count_per_minute:.0f}/min"
    return f"{base} → {band.value}"


def uncertainty_for(cluster: SignalCluster, breakdown: MLScoreBreakdown) -> float:
    """不确定度(0–1):M1 无监督模型→基础偏高;簇条数少→更不确定;监督高分但无统计佐证→略增。"""
    base = 0.3  # M1 无训练标注,监督路先天不确定
    sparse = 0.3 * (1.0 - cluster.confidence)  # 条数少(置信低)→更不确定
    divergence = 0.2 if breakdown.supervised_risk >= 0.5 and breakdown.unsupervised_anomaly == 0 else 0.0
    return round(min(1.0, base + sparse + divergence), 3)


def reasons_for(cluster: SignalCluster, breakdown: MLScoreBreakdown) -> tuple[str, ...]:
    """三路评分原因(可审:每路贡献多少)。"""
    reasons = [
        f"监督路 {breakdown.supervised_risk:.2f}(severity/count/突发/置信)",
        f"无监督路 {breakdown.unsupervised_anomaly:.2f}(统计异常)",
    ]
    if cluster.fan_out > 0:
        reasons.append(f"扇出 {cluster.fan_out} 目标(疑似扫描)")
    return tuple(reasons)


def context_for(cluster: SignalCluster) -> tuple[str, ...]:
    """支撑上下文(给 agent 判断可信度)。"""
    ctx = [f"簇 {cluster.count} 条,置信 {cluster.confidence:.2f}"]
    if cluster.statistical_anomaly > 0:
        ctx.append(f"统计异常分 {cluster.statistical_anomaly:.2f}")
    return tuple(ctx)


def ai_hint_for(band: AnomalyBand, route: CoordinationRoute) -> str:
    """给 agent 的派工提示。"""
    return _ROUTE_HINT.get(route, "")


__all__ = ["narrate", "uncertainty_for", "reasons_for", "context_for", "ai_hint_for"]
