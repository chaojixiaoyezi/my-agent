
from __future__ import annotations

"""ML 引擎 LLM 工具 —— log_ml_analyze(降维+三路评级)+ log_ml_label(outcome 标注回流驯化)。

- log_ml_analyze:候选喂 ml_engine 漏斗(指纹簇+实体簇 → 三路评级),出带解释的异常评级+协调计划;
  评级用**按私有标注自适应调过的融合权重**打分(会学)。
- log_ml_label:给簇打 outcome 标注(success/attempt/failure/other),写私有标注库(owner 隔离),回流让
  下次评级的融合权重自适应——co-teaming 闭环的 M1 落地。

复用 tools.py 的 _LogOpsTool 基类(统一精确错误码)。
"""

import time
from pathlib import Path
from typing import Any

from ..models import BaseTool, ToolSpec
from .tools import _LogOpsTool, _coerce_int, _err, _ok
from ...ml_engine import engine, feedback
from ...ml_engine.models import AnomalyBand, OutcomeLabel, SignalOutcome
from ...ml_engine.reducer import reduce_by_entity, reduce_candidates

_OUTCOME_VALUES = {"success", "attempt", "failure", "other"}
_BAND_VALUES = {"critical", "high", "medium", "low"}


class LogMlAnalyzeTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_ml_analyze",
        category="log_ops",
        effect="read_only",
        description=(
            "对 log_ops 候选队列跑 ML 引擎漏斗(指纹簇+实体簇降维 → 三路融合评级 → 协调派工计划):把海量候选"
            "压成信号簇,按 severity/count/突发/统计异常/**实体扇出**三路评分分级(critical/high/medium/low),"
            "给出带**解释**(narrative/uncertainty/三路分解)的异常评级 + **该派几个子代理研判**。评分用按你的"
            "outcome 标注**自适应调过的融合权重**(见 log_ml_label)。按分降序、截断到 limit。只读,不改候选/存档。"
        ),
        use_cases=[
            "候选积压时先跑 ML 分诊,按评级优先研判 critical/high,别逐条看",
            "多源大量候选要决定派几个子代理、派给谁时,看 coordination_plan",
            "疑似'一个IP扫多个目标'时看实体簇的 fan_out 扇出",
        ],
        avoid_when=[
            "只拉少量候选逐条研判用 log_alert_poll",
            "查单源真实样本用 log_source_sample",
        ],
        keywords=["ML", "评级", "分诊", "降维", "聚类", "triage", "ml_analyze", "异常评分", "派工", "漏斗", "扇出"],
        parameters={
            "limit": "最多返回多少条评级,默认 200,上限 1000",
            "max_candidates": "最多读多少候选参与分析,默认 50000",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={
            "limit": "可选整数,默认 200。",
            "max_candidates": "可选整数,默认 50000(防一次读爆)。",
            "monitor_id": "可选。",
        },
        parameter_schema={
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "max_candidates": {"type": "integer", "minimum": 1, "maximum": 1000000},
            "monitor_id": {"type": "string"},
        },
        required_parameters=[],
        examples=['{"tool": "log_ml_analyze", "limit": 50}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        limit = _coerce_int(params.get("limit"), default=200, lo=1, hi=1000)
        max_cand = _coerce_int(params.get("max_candidates"), default=50000, lo=1, hi=1000000)
        candidates = store.read_candidates(offset=0, limit=max_cand)
        clusters = reduce_candidates(candidates) + reduce_by_entity(candidates)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, feedback.read_labels(store.root))
        result = engine.analyze(clusters, weights=weights, limit=limit)
        payload = result.to_dict()
        payload["ok"] = True
        payload["read_candidates"] = len(candidates)
        payload["fusion_weights"] = weights
        return _ok(self.spec.name, payload)


def _build_label(cluster_id: str, outcome: str, params: dict[str, Any]) -> OutcomeLabel:
    """组装 OutcomeLabel(corrected_band 可选,非法 band 忽略)。"""
    band_raw = str(params.get("corrected_band") or "").strip().lower()
    return OutcomeLabel(
        cluster_id=cluster_id,
        outcome=SignalOutcome(outcome),
        corrected_band=AnomalyBand(band_raw) if band_raw in _BAND_VALUES else None,
        rationale=str(params.get("rationale") or ""),
        labeled_by=str(params.get("labeled_by") or "agent"),
        labeled_at=time.time(),
    )


class LogMlLabelTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_ml_label",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "给一个信号簇打 outcome 标注(success 已得手 / attempt 企图 / failure 被挡 / other),回流驯化 ML "
            "引擎:下次 log_ml_analyze 的三路融合权重据标注自适应(真威胁多→更敏感抓行为异常;误报多→更依赖规则"
            "严重度)。可选 corrected_band 覆盖 ML 评级、rationale 写明理由。标注**私有(owner 隔离),不跨用户**。"
        ),
        use_cases=["研判完一个簇,把结论(成功/企图/误报)回流让引擎学", "ML 评级偏了,用 corrected_band 纠正并留痕"],
        avoid_when=["还没研判清楚别乱标(标错会带歪引擎)", "查评级用 log_ml_analyze"],
        keywords=["标注", "label", "outcome", "回流", "驯化", "纠正", "反馈", "学习", "成功", "企图"],
        parameters={
            "cluster_id": "要标注的簇 id(来自 log_ml_analyze 的 assessments)",
            "outcome": "success/attempt/failure/other",
            "corrected_band": "可选,覆盖评级 critical/high/medium/low",
            "rationale": "可选,标注理由",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"cluster_id": "必填。", "outcome": "必填,四选一。", "corrected_band": "可选。"},
        parameter_schema={
            "cluster_id": {"type": "string"},
            "outcome": {"type": "string", "enum": ["success", "attempt", "failure", "other"]},
            "corrected_band": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "rationale": {"type": "string"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["cluster_id", "outcome"],
        examples=['{"tool": "log_ml_label", "cluster_id": "api1:sql_injection", "outcome": "attempt", "rationale": "确认SQL注入企图,被WAF挡"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        cluster_id = str(params.get("cluster_id") or "").strip()
        outcome = str(params.get("outcome") or "").strip().lower()
        if not cluster_id or outcome not in _OUTCOME_VALUES:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_ml_label 需要 cluster_id 和合法 outcome(success/attempt/failure/other)。")
        store = self.store(params)
        store.ensure_dirs()
        feedback.append_label(store.root, _build_label(cluster_id, outcome, params))
        labels = feedback.read_labels(store.root)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, labels)
        return _ok(self.spec.name, {
            "ok": True, "cluster_id": cluster_id, "outcome": outcome,
            "label_count": len(labels), "adjusted_weights": weights,
            "message": f"已标注,私有标注库现 {len(labels)} 条;下次评级融合权重已自适应。",
        })


def ml_tools(workspace_root: Path) -> list[BaseTool]:
    """ML 引擎工具实例(log_ml_analyze + log_ml_label)。"""
    return [LogMlAnalyzeTool(workspace_root), LogMlLabelTool(workspace_root)]


ML_TOOL_NAMES = ("log_ml_analyze", "log_ml_label")


__all__ = ["ML_TOOL_NAMES", "ml_tools"]
