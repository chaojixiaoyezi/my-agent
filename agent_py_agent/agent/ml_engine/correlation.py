
from __future__ import annotations

"""图关联攻击链(M3-3)——把信号连成攻击链,单步不显连起来才危险。

关联路从占位 0 升级:同一攻击者(src_ip)的告警若涵盖多个 kill chain 阶段(侦察→入侵→提权→外泄),
按推进顺序连成攻击链——单看每步可能不报或低危,连成链才暴露完整攻击意图(高危)。攻击链强度 →
engine 三路的 correlation_boost,把"链上的簇"评级抬高(apply_boosts)。
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from .models import SignalCluster

# kill chain 阶段:triage 规则名 → 阶段(按攻击推进顺序)
_STAGE_MAP = {
    "port_scan": "recon",
    "auth_failure_burst": "access", "brute_force": "access", "sql_injection": "access",
    "injection_generic": "access", "command_injection": "access", "xss": "access", "path_traversal": "access",
    "privilege_escalation": "escalation", "reverse_shell": "escalation",
    "exfiltration": "exfil", "sensitive_file_read": "exfil", "malware_drop": "exfil",
}
_STAGE_ORDER = ("recon", "access", "escalation", "exfil")
_ENTITY_FIELD = "ip"
_MIN_STAGES = 2  # 涵盖 ≥2 个阶段才算攻击链(单步不算)


@dataclass(frozen=True)
class AttackChain:
    """一个攻击者(实体)的攻击链:涵盖的 kill chain 阶段(按序)+ 时间跨度 + 强度。"""

    entity: str
    stages: tuple[str, ...]
    span_seconds: float
    score: float              # 链强度 = 阶段数 / 4(0.5–1.0,阶段越全越危险)
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"entity": self.entity, "stages": list(self.stages), "span_seconds": self.span_seconds,
                "score": self.score, "evidence_refs": list(self.evidence_refs)}


def _record_stages(record: dict[str, Any]) -> set[str]:
    rules = record.get("matched_rules") or []
    return {stage for rule in (rules if isinstance(rules, list) else []) if (stage := _STAGE_MAP.get(str(rule)))}


def _stages_of(members: list[dict[str, Any]]) -> set[str]:
    stages: set[str] = set()
    for member in members:
        stages |= _record_stages(member)
    return stages


def _chain_for(entity: str, members: list[dict[str, Any]]) -> AttackChain | None:
    """一个攻击者的告警 → 攻击链(涵盖 ≥_MIN_STAGES 个阶段才成链)。"""
    ordered = tuple(s for s in _STAGE_ORDER if s in _stages_of(members))
    if len(ordered) < _MIN_STAGES:
        return None
    times = [float(m.get("detected_at") or 0.0) for m in members]
    refs = tuple(str(m.get("fingerprint") or "") for m in members[:5])
    span = (max(times) - min(times)) if times else 0.0
    return AttackChain(entity, ordered, round(span, 3), round(len(ordered) / len(_STAGE_ORDER), 3), refs)


def build_attack_chains(candidates: list[dict[str, Any]]) -> list[AttackChain]:
    """按攻击者实体(ip)聚合,涵盖 ≥2 个 kill chain 阶段 = 攻击链。单步不显,连成链才危险。"""
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        entity = (cand.get("entities") or {}).get(_ENTITY_FIELD)
        if entity:
            by_entity[str(entity)].append(cand)
    chains: list[AttackChain] = []
    for entity, members in by_entity.items():
        chain = _chain_for(entity, members)
        if chain is not None:
            chains.append(chain)
    return chains


def chain_scores_by_entity(chains: list[AttackChain]) -> dict[str, float]:
    """{攻击者实体: 链强度},供 engine 给链上的簇加 correlation_boost。"""
    return {chain.entity: chain.score for chain in chains}


def _boosted(cluster: SignalCluster, chain_scores: dict[str, float]) -> SignalCluster:
    entity = cluster.entities.get(_ENTITY_FIELD)
    boost = chain_scores.get(entity, 0.0) if entity else 0.0
    return replace(cluster, correlation_boost=boost) if boost > 0 else cluster


def apply_boosts(clusters: list[SignalCluster], chain_scores: dict[str, float]) -> list[SignalCluster]:
    """给实体在攻击链上的簇标 correlation_boost(=链强度),返回新簇列表。指纹簇/非链上簇原样。"""
    return [_boosted(cluster, chain_scores) for cluster in clusters]


__all__ = [
    "AttackChain",
    "build_attack_chains",
    "chain_scores_by_entity",
    "apply_boosts",
]
