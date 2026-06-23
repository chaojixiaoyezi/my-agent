
from __future__ import annotations

"""my-agent 内置 ML 推理/决策引擎(设计:docs/design/ML_ENGINE_DESIGN.md)。

填补 log_ops 缺的中段:candidate →(reducer 降维)信号簇 →(engine 三路评级)带解释的异常评级 +
协调派工计划。纯本地、确定性 first-pass、可解释,预留换训练模型口子(MLFeatureVector/
MLSignalAssessment 契约稳定)。默认关(ml_engine_enabled),不破现有规则 triage 路径。
"""

from . import coordination, detection, engine, explain, feedback, reducer, rule_store
from .models import (
    AnomalyBand,
    CoordinationRoute,
    MLAnalysisResult,
    MLCoordinationPlan,
    MLFeatureVector,
    MLScoreBreakdown,
    MLSignalAssessment,
    OutcomeLabel,
    SignalCluster,
    SignalOutcome,
)

__all__ = [
    "coordination",
    "detection",
    "engine",
    "explain",
    "feedback",
    "reducer",
    "rule_store",
    "AnomalyBand",
    "CoordinationRoute",
    "SignalCluster",
    "MLFeatureVector",
    "MLScoreBreakdown",
    "MLSignalAssessment",
    "MLCoordinationPlan",
    "MLAnalysisResult",
    "OutcomeLabel",
    "SignalOutcome",
]
