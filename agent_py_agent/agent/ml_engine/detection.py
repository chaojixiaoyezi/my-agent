
from __future__ import annotations

"""通用声明式检测引擎 —— agent 用 JSON 声明就能监控几十上百种情况,不为每种写代码。

核心洞察(用户纠正):别为"流量突增""扇出扫描"各写专门检测器(那是特化),而是给一套**声明式原语**:
    group_by(按任意实体字段分组) × aggregate(count/distinct/sum/rate) × window(时窗)
    × 判据[absolute 绝对阈值 | deviation 偏离自己基线 K 倍]
agent 组合这些原语就能表达任意情况:
    扇出扫描   = group_by=[ip]  aggregate=distinct(dst)  threshold=50                   (绝对)
    数据外泄   = group_by=[ip]  aggregate=sum(bytes)     window=300 threshold=1e9        (绝对)
    暴力破解   = group_by=[ip]  match=[auth_failure_burst] aggregate=count threshold=20  (绝对)
    流量突增   = group_by=[ip]  aggregate=count  baseline_deviation=true  threshold=3     (偏离自己基线3倍)
    用户数据突增= group_by=[user] aggregate=sum(bytes) baseline_deviation=true threshold=5 (偏离基线5倍)
    API错误突增= group_by=[api] match=[error] aggregate=count baseline_deviation=true     (偏离基线)
    ……几十上百种(含"相对自己基线偏离"那一大类)都是这套的组合,**零专门代码**。

引擎通用作用于任意 records(dict,带 entities/detected_at/matched_rules);M2 喂 candidate,daemon 喂采集行。
absolute 无状态;deviation 用 MetricBaseline(per-group EWMA,冷启动只学不判)——"流量突增"只是它的一个实例。
"""

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_EWMA_ALPHA = 0.3        # 基线 EWMA 平滑系数
_MIN_OBSERVATIONS = 5    # 每分组观测够这么多次才判偏离(冷启动只学)


class AggregateOp(str, Enum):
    """聚合算子(几十种情况的通用积木)。"""

    COUNT = "count"        # 计数(突增/暴力破解)
    DISTINCT = "distinct"  # 去重计数=基数(扇出/端口扫描)
    SUM = "sum"            # 求和(数据外泄字节)
    RATE = "rate"          # 速率=count/窗口分钟(突发)


@dataclass
class MetricBaseline:
    """per group_key 的聚合值 EWMA 基线(通用,不限速率):学每个分组的"正常聚合值",判偏离倍数。

    "流量突增"只是 metric=count 的一个实例;sum(bytes)/distinct(dst) 的偏离同样用它。冷启动(观测<阈值)
    只学不判,避免一开始把什么都当异常。先判后学(否则当前异常会立刻污染基线、把自己的偏离稀释掉)。
    """

    values: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def observe(self, key: str, value: float) -> None:
        prev = self.values.get(key)
        self.values[key] = value if prev is None else (1 - _EWMA_ALPHA) * prev + _EWMA_ALPHA * value
        self.counts[key] = self.counts.get(key, 0) + 1

    def deviation(self, key: str, value: float) -> float:
        """当前值相对基线的偏离倍数(>1=高于基线)。新分组/观测不足返回 0(冷启动只学不判)。"""
        baseline = self.values.get(key)
        if baseline is None or baseline <= 0 or self.counts.get(key, 0) < _MIN_OBSERVATIONS:
            return 0.0
        return round(value / baseline, 3)

    def to_dict(self) -> dict[str, Any]:
        return {"values": self.values, "counts": self.counts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricBaseline":
        return cls(
            values={k: float(v) for k, v in (data.get("values") or {}).items()},
            counts={k: int(v) for k, v in (data.get("counts") or {}).items()},
        )


@dataclass(frozen=True)
class DetectionRule:
    """一条声明式检测规则(数据,非代码)。baseline_deviation=True 时 threshold 是"偏离自己基线的倍数"。"""

    rule_id: str
    name: str
    group_by: tuple[str, ...]
    aggregate: AggregateOp
    agg_field: str = ""
    window_seconds: int = 60
    threshold: float = 0.0
    match_any: tuple[str, ...] = ()
    severity: str = "high"
    baseline_deviation: bool = False   # False=绝对阈值;True=偏离自己 EWMA 基线 threshold 倍
    version: int = 1                   # 规则版本(自适应调阈值时递增,见 rule_feedback)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "name": self.name, "group_by": list(self.group_by),
            "aggregate": self.aggregate.value, "agg_field": self.agg_field,
            "window_seconds": self.window_seconds, "threshold": self.threshold,
            "match_any": list(self.match_any), "severity": self.severity,
            "baseline_deviation": self.baseline_deviation, "version": self.version,
        }


@dataclass(frozen=True)
class DetectionHit:
    """一条命中:某分组在某时窗内聚合值超阈值(或偏离基线超倍数)。"""

    rule_id: str
    rule_name: str
    group_key: str
    agg_value: float
    threshold: float
    window_start: float
    severity: str
    evidence_refs: tuple[str, ...]
    deviation: float = 0.0   # baseline_deviation 模式下的偏离倍数(absolute 模式 0)


@dataclass(frozen=True)
class RuleValidationResult:
    """回测结果:拿历史 record 跑规则,看命中量(估误报)。"""

    rule_id: str
    tested_records: int
    hit_count: int
    distinct_groups_hit: int
    sample_hits: tuple[DetectionHit, ...]


@dataclass(frozen=True)
class _Bucket:
    """一个分组在一个时窗内的聚合结果(收敛参数,避免长参数列表)。"""

    group_key: str
    value: float
    window_start: float
    evidence_refs: tuple[str, ...]


_VALID_AGG = {op.value for op in AggregateOp}


def _coerce_str_list(value: object) -> list[str]:
    """容错:LLM 把"单值 list 字段"误写成裸字符串(group_by='ip' 而非 ['ip'])时包成单元素 list。
    否则 for-in 会把字符串按字符拆(group_by='ip'→('i','p')),规则静默失效、永不命中且不报错。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_rule(data: dict[str, Any]) -> DetectionRule | str:
    """把 agent 的 JSON 声明解析成 DetectionRule;非法返回错误串(可读,给 agent 改)。"""
    group_by = tuple(_coerce_str_list(data.get("group_by")))
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
        return "threshold 必须 > 0(absolute 模式=绝对阈值;deviation 模式=偏离倍数)。"
    return DetectionRule(
        rule_id=str(data.get("rule_id") or "").strip() or f"rule-{abs(hash(str(data))) % 10**8}",
        name=str(data.get("name") or "unnamed").strip(),
        group_by=group_by, aggregate=AggregateOp(agg), agg_field=agg_field,
        window_seconds=max(1, int(_to_float(data.get("window_seconds")) or 60)),
        threshold=threshold,
        match_any=tuple(_coerce_str_list(data.get("match_any"))),
        severity=str(data.get("severity") or "high").strip() or "high",
        baseline_deviation=bool(data.get("baseline_deviation")),
        version=max(1, int(_to_float(data.get("version")) or 1)),
    )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field(record: dict[str, Any], field_name: str) -> str:
    """从 record 取字段值:先查 entities(结构化实体),再查顶层。"""
    ents = record.get("entities") or {}
    return str(ents.get(field_name) or record.get(field_name) or "")


def _passes_filter(rule: DetectionRule, record: dict[str, Any]) -> bool:
    if not rule.match_any:
        return True
    matched = record.get("matched_rules") or []
    names = set(matched) if isinstance(matched, list) else set()
    return bool(names & set(rule.match_any))


def _group_key(rule: DetectionRule, record: dict[str, Any]) -> str | None:
    """该 record 的分组键;任一 group_by 字段缺失则不入组(返回 None)。"""
    parts = []
    for field_name in rule.group_by:
        value = _field(record, field_name)
        if not value:
            return None
        parts.append(f"{field_name}={value}")
    return "&".join(parts)


def _aggregate(rule: DetectionRule, members: list[dict[str, Any]]) -> float:
    if rule.aggregate == AggregateOp.COUNT:
        return float(len(members))
    if rule.aggregate == AggregateOp.RATE:
        return len(members) / max(rule.window_seconds / 60.0, 1e-9)
    if rule.aggregate == AggregateOp.DISTINCT:
        return float(len({v for m in members if (v := _field(m, rule.agg_field))}))
    return sum(_to_float(_field(m, rule.agg_field)) for m in members)  # SUM


def _buckets(rule: DetectionRule, group_key: str, members: list[dict[str, Any]]) -> list[_Bucket]:
    """把一个分组按对齐时窗分桶,每桶算聚合值 + 起始时间 + 证据。"""
    by_window: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for member in members:
        by_window[int(float(member.get("detected_at") or 0.0) // rule.window_seconds)].append(member)
    out: list[_Bucket] = []
    for bucket_members in by_window.values():
        start = min((float(m.get("detected_at") or 0.0) for m in bucket_members), default=0.0)
        refs = tuple(str(m.get("fingerprint") or "") for m in bucket_members[:5])
        out.append(_Bucket(group_key, _aggregate(rule, bucket_members), start, refs))
    return out


def _hit(rule: DetectionRule, bucket: _Bucket, deviation: float) -> DetectionHit:
    return DetectionHit(rule.rule_id, rule.name, bucket.group_key, bucket.value, rule.threshold,
                        bucket.window_start, rule.severity, bucket.evidence_refs, deviation)


def _check_bucket(rule: DetectionRule, bucket: _Bucket, baseline: MetricBaseline | None) -> DetectionHit | None:
    """判一个桶是否命中:deviation 模式比偏离倍数(并续学基线),absolute 模式比绝对阈值。"""
    if rule.baseline_deviation:
        if baseline is None:
            return None
        dev = baseline.deviation(bucket.group_key, bucket.value)
        baseline.observe(bucket.group_key, bucket.value)  # 先判后学
        return _hit(rule, bucket, dev) if dev >= rule.threshold else None
    return _hit(rule, bucket, 0.0) if bucket.value > rule.threshold else None


def _group_hits(rule: DetectionRule, group_key: str, members: list[dict[str, Any]], baseline: MetricBaseline | None) -> list[DetectionHit]:
    """一个分组各时窗桶的命中(抽出来,避免 evaluate_rule 三层嵌套)。"""
    out: list[DetectionHit] = []
    for bucket in _buckets(rule, group_key, members):
        hit = _check_bucket(rule, bucket, baseline)
        if hit is not None:
            out.append(hit)
    return out


def evaluate_rule(rule: DetectionRule, records: list[dict[str, Any]], baseline: MetricBaseline | None = None) -> list[DetectionHit]:
    """通用评估:filter → group_by → 时窗聚合 → 判据(绝对阈值 或 偏离自己基线)→ 命中。覆盖几十上百种情况。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not isinstance(record, dict) or not _passes_filter(rule, record):
            continue
        key = _group_key(rule, record)
        if key is not None:
            groups[key].append(record)
    hits: list[DetectionHit] = []
    for group_key, members in groups.items():
        hits.extend(_group_hits(rule, group_key, members, baseline))
    return hits


def backtest_rule(rule: DetectionRule, historical_records: list[dict[str, Any]]) -> RuleValidationResult:
    """回测:拿历史 record 跑规则,统计命中量(命中分组数估误报面),达标才该上线。deviation 规则用临时基线。"""
    baseline = MetricBaseline() if rule.baseline_deviation else None
    hits = evaluate_rule(rule, historical_records, baseline)
    groups_hit = len({h.group_key for h in hits})
    return RuleValidationResult(rule.rule_id, len(historical_records), len(hits), groups_hit, tuple(hits[:5]))


__all__ = [
    "AggregateOp",
    "MetricBaseline",
    "DetectionRule",
    "DetectionHit",
    "RuleValidationResult",
    "parse_rule",
    "evaluate_rule",
    "backtest_rule",
]
