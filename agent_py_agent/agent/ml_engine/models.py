
from __future__ import annotations

"""ML 引擎核心数据结构 —— 降维信号簇 / 特征向量 / 评级输出 / 标注 / 总结果。

定位:ML 推理/决策引擎的**契约层**(纯 dataclass,无 IO 无副作用)。
数据流:candidate(log_ops triage 产的 dict)→ SignalCluster(②降维)→ MLFeatureVector(③特征)
→ MLScoreBreakdown(三路分解)→ MLSignalAssessment(评级+解释)→ MLAnalysisResult(总输出)。

两个设计要点:
1. **契约即换模型口子**:MLFeatureVector(输入)+MLSignalAssessment(输出)稳定;engine 换训练
   模型只重写打分,这套 dataclass 与上下游全不动(claw 留的口子)。
2. **Enum 继承 str**:AnomalyBand/CoordinationRoute/SignalOutcome 都是 str 子类,json.dumps
   直接可序列化成字符串值(工具层 _ok 不必特判枚举),to_dict 用 dataclasses.asdict 即可。
"""

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AnomalyBand(str, Enum):
    """异常分级(评分→band)。CRITICAL 升主代理,HIGH 子代理域审,MEDIUM/LOW 临时孙代理扫证据。"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CoordinationRoute(str, Enum):
    """协调路由:按 band 决定派工去向(对接 my-agent 三层代理)。"""

    MAIN_AGENT_ESCALATION = "main_agent_escalation"
    CHILD_DOMAIN_REVIEW = "child_domain_review"
    TEMP_GRANDCHILD_EVIDENCE_SCAN = "temp_grandchild_evidence_scan"


class SignalOutcome(str, Enum):
    """研判结论标签(agent 回流的监督信号):成功 > 企图 > 失败 > 其他。评级目标=预测会不会成功。"""

    SUCCESS = "success"
    ATTEMPT = "attempt"
    FAILURE = "failure"
    OTHER = "other"


@dataclass(frozen=True)
class SignalCluster:
    """②降维单元:一批同指纹(或同实体)的 candidate 聚合后的信号簇。

    cluster_kind 区分两种聚类键:"fingerprint"(按规则指纹聚,抓重复模式)与 "entity"(按实体如
    src_ip 聚,抓扇出扫描)。entity/distinct_targets/fan_out 是 UEBA 字段(fingerprint 簇为空/0)。
    """

    cluster_id: str
    domain_id: str
    fingerprint: str
    cluster_kind: str  # "fingerprint" | "entity"
    severity: int  # 1–5(由 candidate.severity high/medium/low 映射)
    count: int
    confidence: float
    first_ts: float
    last_ts: float
    entities: dict[str, str] = field(default_factory=dict)  # UEBA:src_ip/ip/user/...
    distinct_targets: int = 0  # UEBA:实体簇下不同目标数
    fan_out: int = 0  # UEBA:扇出度(一个实体打了多少不同目标)
    evidence_refs: tuple[str, ...] = ()  # 回存档看原始行的钩子(capped)
    statistical_anomaly: float = 0.0  # 复用 baseline 统计异常分(0–1)
    correlation_boost: float = 0.0  # 关联路:该簇实体在攻击链上的强度(M3-3,见 correlation)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.last_ts - self.first_ts)

    @property
    def count_per_minute(self) -> float:
        dur = self.duration_seconds
        return self.count / (dur / 60.0) if dur > 0 else float(self.count)


@dataclass(frozen=True)
class MLFeatureVector:
    """③推理统一输入契约(换训练模型不动这层)。"""

    cluster_id: str
    domain_id: str
    fingerprint: str
    alert_type: str
    severity: int
    count: int
    confidence: float
    duration_seconds: float
    count_per_minute: float
    cardinality: int  # UEBA:基数(实体簇=distinct_targets,指纹簇=0)
    fan_out: int  # UEBA:扇出度
    rate_deviation: float  # UEBA:速率偏离基线(M1 占位 0,M2 接 per-entity 时序)
    statistical_anomaly: float
    correlation_boost: float = 0.0  # 关联路:攻击链强度(M3-3)


@dataclass(frozen=True)
class MLScoreBreakdown:
    """三路评分分解(可解释:每路各贡献多少)。"""

    supervised_risk: float
    unsupervised_anomaly: float
    correlation_boost: float
    fused_score: float


@dataclass(frozen=True)
class MLSignalAssessment:
    """③单簇评级输出(带解释层 narrative/uncertainty,防裸分数过度/不足信任)。"""

    cluster_id: str
    domain_id: str
    fused_score: float
    band: AnomalyBand
    route: CoordinationRoute
    breakdown: MLScoreBreakdown
    predicted_outcome: SignalOutcome
    outcome_confidence: float
    feature: MLFeatureVector
    narrative: str
    uncertainty: float
    supporting_context: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    ai_hint: str = ""


@dataclass(frozen=True)
class MLCoordinationPlan:
    """④协调派工计划(M1 只产计划,M2 才接 dispatch 实际派工)。"""

    temporary_worker_count: int
    domain_clusters: dict[str, list[str]]
    route_counts: dict[str, int]
    band_counts: dict[str, int]
    main_agent_note: str = ""


@dataclass(frozen=True)
class MLAnalysisResult:
    """总输出契约(log_ml_analyze 返回)。assessments 已按 fused_score 降序、截断到 limit。"""

    total_candidates: int
    shard_count: int
    total_cluster_count: int
    assessment_limit: int
    omitted_assessment_count: int
    assessments: tuple[MLSignalAssessment, ...]
    coordination_plan: MLCoordinationPlan

    def to_dict(self) -> dict[str, Any]:
        """序列化给工具层。Enum 都是 str 子类,asdict 后 json.dumps 直接出字符串值。"""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class OutcomeLabel:
    """②agent 复核回流的监督标注(驯化引擎);写盘到 owner-scoped 私有标注库。"""

    cluster_id: str
    outcome: SignalOutcome
    corrected_band: AnomalyBand | None
    rationale: str
    labeled_by: str
    labeled_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "outcome": self.outcome.value,
            "corrected_band": self.corrected_band.value if self.corrected_band else None,
            "rationale": self.rationale,
            "labeled_by": self.labeled_by,
            "labeled_at": self.labeled_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutcomeLabel":
        band = data.get("corrected_band")
        return cls(
            cluster_id=str(data.get("cluster_id") or ""),
            outcome=SignalOutcome(str(data.get("outcome") or "other")),
            corrected_band=AnomalyBand(str(band)) if band else None,
            rationale=str(data.get("rationale") or ""),
            labeled_by=str(data.get("labeled_by") or ""),
            labeled_at=float(data.get("labeled_at") or 0.0),
        )


__all__ = [
    "AnomalyBand",
    "CoordinationRoute",
    "SignalOutcome",
    "SignalCluster",
    "MLFeatureVector",
    "MLScoreBreakdown",
    "MLSignalAssessment",
    "MLCoordinationPlan",
    "MLAnalysisResult",
    "OutcomeLabel",
]
