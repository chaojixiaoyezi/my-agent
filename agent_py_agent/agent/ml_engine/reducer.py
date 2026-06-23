
from __future__ import annotations

"""②降维:log_ops candidate(dict)→ SignalCluster。两种正交聚类键:

1. **指纹簇** reduce_candidates:按(source_id : 命中规则组合)聚——同源同类告警归一簇,抓重复模式。
   (聚类键不是 candidate.fingerprint,那是 per-line 唯一指纹,每行不同压不动。)
2. **实体簇** reduce_by_entity:按(source_id : 源实体 ip)聚——一个 IP 的所有告警归一簇,算扇出
   (打了多少不同目标),抓"一个 IP 扫多个 IP/端口"这类横向扫描。这类在指纹簇里会被打散看不出。

两键正交、可并行喂进 engine 评级(engine.analyze 的 total_candidates 只数指纹簇,不重复计)。
severity 由 high/medium/low 映射成 1–5;statistical_anomaly 取簇内最高;evidence_refs 取簇内
candidate 的 per-line 指纹(回存档看原始行的钩子,capped)。
"""

import math
from collections import defaultdict
from typing import Any

from .models import SignalCluster

_SEVERITY_MAP = {"high": 5, "medium": 3, "low": 1}
_MAX_EVIDENCE_REFS = 5
_ENTITY_KEY = "ip"  # 主实体(源)字段:extract_entities 把首个 IP 归到这
# 扇出目标候选字段(取首个存在的):一个源实体打了多少不同目标 = 扫描扇出度
_TARGET_KEYS = ("dst_ip", "dst", "target", "dport", "port", "url", "path", "user")


def _severity_int(text: Any) -> int:
    return _SEVERITY_MAP.get(str(text or "").strip().lower(), 1)


def _confidence_for(count: int) -> float:
    """簇置信度随条数增长(0.5→1.0):越多同类越确信不是偶发噪声。"""
    return round(min(1.0, 0.5 + 0.5 * math.log1p(count) / math.log1p(1000)), 3)


def _agg_severity_anomaly(members: list[dict[str, Any]]) -> tuple[int, float]:
    """簇内聚合:取最高严重度 + 最高统计异常分。"""
    sev = max((_severity_int(m.get("severity")) for m in members), default=1)
    anomaly = max((float(m.get("anomaly_score") or 0.0) for m in members), default=0.0)
    return sev, round(anomaly, 3)


def _evidence_refs(members: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(m.get("fingerprint") or "") for m in members[:_MAX_EVIDENCE_REFS])


def _rule_fingerprint(candidate: dict[str, Any]) -> str:
    """聚类指纹 = 命中规则名排序拼接(同类告警同指纹);无规则则 uncategorized。"""
    rules = candidate.get("matched_rules")
    names = sorted(str(r) for r in rules) if isinstance(rules, list) else []
    return "+".join(names) or "uncategorized"


def _build_cluster(domain: str, fingerprint: str, members: list[dict[str, Any]]) -> SignalCluster:
    """一组同(源:指纹)candidate → 一个指纹簇(窗口聚合 + 取最高严重度/异常分)。"""
    sev, anomaly = _agg_severity_anomaly(members)
    times = [float(m.get("detected_at") or 0.0) for m in members]
    return SignalCluster(
        cluster_id=f"{domain}:{fingerprint}",
        domain_id=domain,
        fingerprint=fingerprint,
        cluster_kind="fingerprint",
        severity=sev,
        count=len(members),
        confidence=_confidence_for(len(members)),
        first_ts=min(times, default=0.0),
        last_ts=max(times, default=0.0),
        evidence_refs=_evidence_refs(members),
        statistical_anomaly=anomaly,
    )


def reduce_candidates(candidates: list[dict[str, Any]]) -> list[SignalCluster]:
    """按(source_id:规则指纹)把 candidate 聚成指纹簇。坏项跳过;空输入→空列表。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        domain = str(cand.get("source_id") or "unknown")
        groups[(domain, _rule_fingerprint(cand))].append(cand)
    return [_build_cluster(domain, fp, members) for (domain, fp), members in groups.items()]


def _target_value(candidate: dict[str, Any]) -> str | None:
    """该 candidate 的'目标'值(扇出对象):取首个存在的目标类字段。"""
    ents = candidate.get("entities") or {}
    for key in _TARGET_KEYS:
        val = ents.get(key)
        if val:
            return str(val)
    return None


def _build_entity_cluster(domain: str, ip: str, members: list[dict[str, Any]]) -> SignalCluster:
    """一个源实体(ip)的所有告警 → 实体簇,扇出 = 簇内不同目标值数(抓'一个IP打多个目标'扫描)。"""
    targets = {t for m in members if (t := _target_value(m))}
    sev, anomaly = _agg_severity_anomaly(members)
    times = [float(m.get("detected_at") or 0.0) for m in members]
    return SignalCluster(
        cluster_id=f"{domain}:entity:{ip}",
        domain_id=domain,
        fingerprint=f"entity:{ip}",
        cluster_kind="entity",
        severity=sev,
        count=len(members),
        confidence=_confidence_for(len(members)),
        first_ts=min(times, default=0.0),
        last_ts=max(times, default=0.0),
        entities={_ENTITY_KEY: ip},
        distinct_targets=len(targets),
        fan_out=len(targets),
        evidence_refs=_evidence_refs(members),
        statistical_anomaly=anomaly,
    )


def reduce_by_entity(candidates: list[dict[str, Any]]) -> list[SignalCluster]:
    """按源实体(entities.ip)聚合 → 实体簇,带扇出度。无实体的 candidate 不进。

    与指纹簇正交:'一个IP扫多个目标'在指纹簇里被规则打散看不出,实体簇按 IP 聚才显出扇出。
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ip = (cand.get("entities") or {}).get(_ENTITY_KEY)
        if not ip:
            continue
        groups[(str(cand.get("source_id") or "unknown"), str(ip))].append(cand)
    return [_build_entity_cluster(domain, ip, members) for (domain, ip), members in groups.items()]


def shard_count(clusters: list[SignalCluster]) -> int:
    """分片数 = 不同 domain 数(M1 按 domain 逻辑分片,不真并行)。"""
    return len({c.domain_id for c in clusters})


__all__ = ["reduce_candidates", "reduce_by_entity", "shard_count"]
