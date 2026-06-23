
from __future__ import annotations

"""通用声明式检测引擎 —— agent 用 JSON 声明就能监控几十上百种情况,不为每种写代码。

核心洞察(用户纠正):别为"流量突增""扇出扫描"各写专门检测器(那是特化),而是给一套**声明式原语**:
    group_by(按任意实体字段分组) × aggregate(count/distinct/sum/rate) × window(时窗) × threshold(阈值)
agent 组合这些原语就能表达任意情况:
    流量突增   = group_by=[ip]        aggregate=count             window=60  threshold=500
    横向扫描   = group_by=[ip]        aggregate=distinct(dst)     window=60  threshold=50
    端口扫描   = group_by=[ip]        aggregate=distinct(dport)   window=60  threshold=100
    数据外泄   = group_by=[ip]        aggregate=sum(bytes)        window=300 threshold=1e9
    暴力破解   = group_by=[ip] match=[auth_failure_burst] aggregate=count window=60 threshold=20
    ……几十上百种都是这套的组合,**零专门代码**。

引擎通用作用于任意 records(dict,带 entities/detected_at/matched_rules);M2-2 喂 candidate,M2-4 daemon
集成可喂采集行。绝对阈值(>threshold)覆盖大部分;偏离基线 K 倍(per-entity baseline)留后续扩展。
"""

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AggregateOp(str, Enum):
    """聚合算子(几十种情况的通用积木)。"""

    COUNT = "count"        # 计数(突增/暴力破解)
    DISTINCT = "distinct"  # 去重计数=基数(扇出/端口扫描)
    SUM = "sum"            # 求和(数据外泄字节)
    RATE = "rate"          # 速率=count/窗口分钟(突发)


@dataclass(frozen=True)
class DetectionRule:
    """一条声明式检测规则(数据,非代码)。agent 用 JSON 声明,系统通用评估。"""

    rule_id: str
    name: str
    group_by: tuple[str, ...]          # 按哪些实体字段分组,如 ("ip",) / ("ip", "dport")
    aggregate: AggregateOp
    agg_field: str = ""                # distinct/sum 的目标字段(count/rate 不需要)
    window_seconds: int = 60
    threshold: float = 0.0
    match_any: tuple[str, ...] = ()    # 可选:只统计 matched_rules 命中这些的 record(filter)
    severity: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "name": self.name, "group_by": list(self.group_by),
            "aggregate": self.aggregate.value, "agg_field": self.agg_field,
            "window_seconds": self.window_seconds, "threshold": self.threshold,
            "match_any": list(self.match_any), "severity": self.severity,
        }


@dataclass(frozen=True)
class DetectionHit:
    """一条命中:某分组在某时窗内聚合值超阈值。"""

    rule_id: str
    rule_name: str
    group_key: str
    agg_value: float
    threshold: float
    window_start: float
    severity: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class RuleValidationResult:
    """回测结果:拿历史 record 跑规则,看命中量(估误报)。"""

    rule_id: str
    tested_records: int
    hit_count: int
    distinct_groups_hit: int
    sample_hits: tuple[DetectionHit, ...]


_VALID_AGG = {op.value for op in AggregateOp}


def parse_rule(data: dict[str, Any]) -> DetectionRule | str:
    """把 agent 的 JSON 声明解析成 DetectionRule;非法返回错误串(可读,给 agent 改)。"""
    group_by = tuple(str(g).strip() for g in (data.get("group_by") or []) if str(g).strip())
    agg = str(data.get("aggregate") or "").strip().lower()
    agg_field = str(data.get("agg_field") or "").strip()
    if not group_by:
        return "group_by 不能为空(至少按一个字段分组,如 ['ip'])。"
    if agg not in _VALID_AGG:
        return f"aggregate 必须是 {sorted(_VALID_AGG)} 之一。"
    if agg in ("distinct", "sum") and not agg_field:
        return f"aggregate={agg} 需要 agg_field(对哪个字段去重/求和)。"
    threshold = _to_float(data.get("threshold"))
    if threshold <= 0:
        return "threshold 必须 > 0。"
    return DetectionRule(
        rule_id=str(data.get("rule_id") or "").strip() or f"rule-{abs(hash(str(data))) % 10**8}",
        name=str(data.get("name") or "unnamed").strip(),
        group_by=group_by, aggregate=AggregateOp(agg), agg_field=agg_field,
        window_seconds=max(1, int(_to_float(data.get("window_seconds")) or 60)),
        threshold=threshold,
        match_any=tuple(str(m).strip() for m in (data.get("match_any") or []) if str(m).strip()),
        severity=str(data.get("severity") or "high").strip() or "high",
    )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field(record: dict[str, Any], field: str) -> str:
    """从 record 取字段值:先查 entities(结构化实体),再查顶层。"""
    ents = record.get("entities") or {}
    return str(ents.get(field) or record.get(field) or "")


def _passes_filter(rule: DetectionRule, record: dict[str, Any]) -> bool:
    if not rule.match_any:
        return True
    matched = record.get("matched_rules") or []
    names = set(matched) if isinstance(matched, list) else set()
    return bool(names & set(rule.match_any))


def _group_key(rule: DetectionRule, record: dict[str, Any]) -> str | None:
    """该 record 的分组键(group_by 各字段值拼接);任一字段缺失则不入组(返回 None)。"""
    parts = []
    for field in rule.group_by:
        value = _field(record, field)
        if not value:
            return None
        parts.append(f"{field}={value}")
    return "&".join(parts)


def _aggregate(rule: DetectionRule, members: list[dict[str, Any]]) -> float:
    if rule.aggregate == AggregateOp.COUNT:
        return float(len(members))
    if rule.aggregate == AggregateOp.RATE:
        return len(members) / max(rule.window_seconds / 60.0, 1e-9)
    if rule.aggregate == AggregateOp.DISTINCT:
        return float(len({v for m in members if (v := _field(m, rule.agg_field))}))
    return sum(_to_float(_field(m, rule.agg_field)) for m in members)  # SUM


def _make_hit(rule: DetectionRule, group_key: str, value: float, members: list[dict[str, Any]]) -> DetectionHit:
    refs = tuple(str(m.get("fingerprint") or "") for m in members[:5])
    start = min((float(m.get("detected_at") or 0.0) for m in members), default=0.0)
    return DetectionHit(rule.rule_id, rule.name, group_key, value, rule.threshold, start, rule.severity, refs)


def _eval_group(rule: DetectionRule, group_key: str, members: list[dict[str, Any]]) -> list[DetectionHit]:
    """一个分组按对齐时窗分桶,每桶聚合判阈值。"""
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for member in members:
        buckets[int(float(member.get("detected_at") or 0.0) // rule.window_seconds)].append(member)
    hits = []
    for bucket_members in buckets.values():
        value = _aggregate(rule, bucket_members)
        if value > rule.threshold:
            hits.append(_make_hit(rule, group_key, value, bucket_members))
    return hits


def evaluate_rule(rule: DetectionRule, records: list[dict[str, Any]]) -> list[DetectionHit]:
    """通用评估:filter → group_by → 时窗聚合 → 判阈值 → 命中。这套覆盖几十上百种情况。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict) or not _passes_filter(rule, record):
            continue
        key = _group_key(rule, record)
        if key is not None:
            groups[key].append(record)
    hits: list[DetectionHit] = []
    for group_key, members in groups.items():
        hits.extend(_eval_group(rule, group_key, members))
    return hits


def backtest_rule(rule: DetectionRule, historical_records: list[dict[str, Any]]) -> RuleValidationResult:
    """回测:拿历史 record 跑规则,统计命中量(命中分组数估误报面),达标才该上线。"""
    hits = evaluate_rule(rule, historical_records)
    groups_hit = len({h.group_key for h in hits})
    return RuleValidationResult(rule.rule_id, len(historical_records), len(hits), groups_hit, tuple(hits[:5]))


__all__ = [
    "AggregateOp",
    "DetectionRule",
    "DetectionHit",
    "RuleValidationResult",
    "parse_rule",
    "evaluate_rule",
    "backtest_rule",
]
