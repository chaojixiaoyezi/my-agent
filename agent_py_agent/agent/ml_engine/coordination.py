
from __future__ import annotations

"""④协调派工:把 ML 评级翻译成真实子代理派工意图(纯逻辑,可单测)。

assessments → DispatchDirective:
- worker_items:非 LOW 按 domain 聚合,每个 domain 一个派工 item(goal 含簇+证据+研判要求,
  attributes 带 ml_domain/ml_clusters/ml_evidence_refs),受 max_runners 截断 → 工具层喂给
  _execute_create_subagents 真派工(复用完整 lifecycle/auto_start)。
- escalations:CRITICAL 簇升级信息(工具层报告;自动开 collaboration case 需 thread 上下文,M2 后续)。

这是 M1 评级落地为行动——替代夜测暴露的"派工靠主代理 LLM 拍脑袋/默认 single_worker"。
"""

from dataclasses import dataclass
from typing import Any

from .models import AnomalyBand, MLSignalAssessment

_ROLE_BY_BAND = {
    AnomalyBand.HIGH: "log_domain_review",
    AnomalyBand.MEDIUM: "log_evidence_scan",
    AnomalyBand.LOW: "log_evidence_scan",
}
_MAX_CLUSTERS_PER_ITEM = 20
_MAX_EVIDENCE_PER_ITEM = 10


@dataclass(frozen=True)
class DispatchDirective:
    """派工意图:worker_items 给 create_subagents 真派工,escalations 是 CRITICAL 升级信息。"""

    worker_items: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    skipped_low: int


def _accumulate(groups: dict[str, dict[str, Any]], assessment: MLSignalAssessment) -> None:
    """把一条非 LOW 评级并入它的 domain 分组(取最高 band)。"""
    entry = groups.setdefault(
        assessment.domain_id,
        {"band": assessment.band, "clusters": [], "evidence": [], "notes": []},
    )
    if _band_rank(assessment.band) > _band_rank(entry["band"]):
        entry["band"] = assessment.band
    entry["clusters"].append(assessment.cluster_id)
    entry["evidence"].extend(assessment.evidence_refs)
    entry["notes"].append(assessment.narrative)


_BAND_ORDER = {AnomalyBand.LOW: 0, AnomalyBand.MEDIUM: 1, AnomalyBand.HIGH: 2, AnomalyBand.CRITICAL: 3}


def _band_rank(band: AnomalyBand) -> int:
    return _BAND_ORDER.get(band, 0)


def _worker_item(domain: str, info: dict[str, Any]) -> dict[str, Any]:
    """一个 domain 的派工 item:goal 含该域簇数+线索+研判要求,attributes 带回溯钩子。"""
    band: AnomalyBand = info["band"]
    goal = (
        f"研判源 {domain} 的 {len(info['clusters'])} 个异常信号簇(最高 {band.value})。"
        f"线索:{'; '.join(info['notes'][:3])}。"
        f"用 log_source_query 按 evidence_refs 回存档查原始行核实,确认真实威胁后用 log_report 分级上报。"
    )
    return {
        "goal": goal,
        "role": _ROLE_BY_BAND.get(band, "log_evidence_scan"),
        "attributes": {
            "ml_domain": domain,
            "ml_clusters": info["clusters"][:_MAX_CLUSTERS_PER_ITEM],
            "ml_evidence_refs": info["evidence"][:_MAX_EVIDENCE_PER_ITEM],
            "ml_band": band.value,
        },
    }


def plan_dispatch(assessments: list[MLSignalAssessment], max_runners: int = 16) -> DispatchDirective:
    """评级 → 派工意图:非 LOW 按 domain 聚合派子代理(受 max_runners),CRITICAL 额外升级。"""
    groups: dict[str, dict[str, Any]] = {}
    skipped = 0
    for assessment in assessments:
        if assessment.band == AnomalyBand.LOW:
            skipped += 1
            continue
        _accumulate(groups, assessment)
    items = [_worker_item(domain, info) for domain, info in groups.items()][: max(0, max_runners)]
    escalations = [
        {"domain": a.domain_id, "cluster_id": a.cluster_id, "narrative": a.narrative, "score": a.fused_score}
        for a in assessments
        if a.band == AnomalyBand.CRITICAL
    ]
    return DispatchDirective(worker_items=items, escalations=escalations, skipped_low=skipped)


__all__ = ["DispatchDirective", "plan_dispatch"]
