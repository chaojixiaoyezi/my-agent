
from __future__ import annotations

"""统计基线 + 数据驱动异常检测 —— 初筛不只靠死正则,补"偏离正常"判据(新实体 / 罕见值)。

安全初筛纯正则的根本短板:规则定太宽就误报淹没(实测 unusual-ssh-user 占候选 74%),定太窄又漏新型。
数据驱动做法:先学每源的"正常长相"(各低基数字段的正常值集合),再标"没见过的实体 / 罕见值"——新攻击者、
新用户、罕见操作天然浮出,不靠人猜规则。高基数字段(时间戳 / 唯一 id)自动识别并跳过(每条都新,无意义)。
这是 UEBA(用户实体行为分析)的最小内核:实体首现检测 + 罕见度,启发式起步,不上重模型,符合长跑轻量。
"""

import re
from dataclasses import dataclass, field
from typing import Any

from ...common import schema_version
from ...common.json_io import read_json_object, write_json_file_atomic
from .store import LogOpsStore

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_KV = re.compile(r'"?([A-Za-z_][\w.]*)"?\s*[=:]\s*"?([^\s",}\]]+)"?')  # 兼容 key=value 和 JSON "key":"value"
_VALUES_CAP = 256  # 每字段最多记多少不同值(防高基数字段把基线撑爆)
_HIGH_CARD_RATIO = 0.6  # distinct/total 超过此值 = 高基数字段(时间戳/id),跳过新实体检测
_RARE_RATIO = 0.01  # 值出现占比低于此 = 罕见值
_DECAY_WINDOW = 100_000  # 超过这么多条记录没再出现的值从基线淘汰(正常模式漂移适应 + 攻击污染可恢复)
# baseline schema 版本(v1=带 last_seen 衰减字段;无戳的旧基线视为 v0,from_dict 兜底 last_seen 为空)。
_BASELINE_SCHEMA_VERSION = 1
_BASELINE_MIGRATIONS: dict[int, schema_version.Migration] = {}
_TS_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")  # 抹时间戳,免 T11:23 误当 kv


def extract_entities(raw_line: str) -> dict[str, str]:
    """提取关键实体字段:首个 IP(归 ip 字段) + key=value / key:value 对。先抹掉时间戳,避免 '...T11:23:45' 被
    误当 kv(t11=23) 制造高基数噪声(实测把候选撑到 200 万)。用于新实体 / 罕见值检测。"""
    cleaned = _TS_PATTERN.sub(" ", raw_line)
    ents: dict[str, str] = {}
    ips = _IPV4.findall(cleaned)
    if ips:
        ents["ip"] = ips[0]
    for key, val in _KV.findall(cleaned):
        low = key.lower()
        if low not in ents:  # 同名字段取首个值
            ents[low] = val
    return ents


@dataclass
class _FieldStat:
    """一个字段的统计:见过的值 -> 计数(基数受 _VALUES_CAP 限) + 每值最后出现的记录序号(衰减用) + 总观测数。"""

    values: dict[str, int] = field(default_factory=dict)
    total: int = 0
    last_seen: dict[str, int] = field(default_factory=dict)  # 值 -> 最后出现的记录序号(LRU/TTL 衰减用)

    def observe(self, value: str, seq: int) -> None:
        self.total += 1
        if value in self.values:
            self.values[value] += 1
        elif len(self.values) < _VALUES_CAP:
            self.values[value] = 1
        else:
            # 满 cap:淘汰最久未见的值(LRU;旧基线无 last_seen 的老值优先淘汰),给新值腾位。
            # 让基线随"正常"漂移——旧值(含被误学的攻击实体)不永久占位,正常新实体也进得来不被永久误报。
            oldest = min(self.values, key=lambda v: self.last_seen.get(v, -1))
            self.values.pop(oldest, None)
            self.last_seen.pop(oldest, None)
            self.values[value] = 1
        self.last_seen[value] = seq

    def decay(self, now_seq: int, window: int) -> int:
        """淘汰 window 条记录内没再出现的值(正常漂移适应 + 攻击污染可恢复)。返回淘汰数。"""
        stale = [v for v, seq in self.last_seen.items() if now_seq - seq > window]
        for v in stale:
            self.values.pop(v, None)
            self.last_seen.pop(v, None)
        return len(stale)

    def is_high_card(self) -> bool:
        """高基数字段(几乎每条都不同,如时间戳/uuid/pid):不适合做新实体检测。
        关键:值种类达到 cap 必判高基数——否则 cap 把 distinct 钉在上限,distinct/total 被大 total 稀释成假低基数,
        导致每个新时间戳都被当'新实体'误报(实测时间戳类误报占候选 99%+,把候选撑到 200 万)。"""
        return len(self.values) >= _VALUES_CAP or (self.total >= 20 and len(self.values) / self.total > _HIGH_CARD_RATIO)


@dataclass
class SourceBaseline:
    """一个源的统计基线:各关键字段的正常值集合 + 计数。学正常 → 判偏离。"""

    fields: dict[str, _FieldStat] = field(default_factory=dict)
    records: int = 0

    def observe_line(self, raw_line: str) -> None:
        """用一条正常流量更新基线(daemon 持续学,滑动地认识"正常")。"""
        self.records += 1
        for key, val in extract_entities(raw_line).items():
            self.fields.setdefault(key, _FieldStat()).observe(val, self.records)

    def decay(self, window: int = _DECAY_WINDOW) -> int:
        """周期调用:淘汰各字段 window 条记录内未再现的值,适应正常漂移、让攻击污染可恢复。返回总淘汰数。"""
        return sum(fs.decay(self.records, window) for fs in self.fields.values())

    def to_dict(self) -> dict[str, Any]:
        return schema_version.stamp(
            {
                "records": self.records,
                "fields": {
                    k: {"values": fs.values, "total": fs.total, "last_seen": fs.last_seen}
                    for k, fs in self.fields.items()
                },
            },
            _BASELINE_SCHEMA_VERSION,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceBaseline":
        data = schema_version.migrate(
            dict(data), current_version=_BASELINE_SCHEMA_VERSION, migrations=_BASELINE_MIGRATIONS
        )
        obj = cls(records=int(data.get("records") or 0))
        for key, fdict in (data.get("fields") or {}).items():
            obj.fields[key] = _FieldStat(
                values=dict(fdict.get("values") or {}),
                total=int(fdict.get("total") or 0),
                last_seen={k: int(v) for k, v in (fdict.get("last_seen") or {}).items()},
            )
        return obj


def build_baseline(lines: list[str]) -> SourceBaseline:
    """从一批样本(探查/备课采样)建初始基线。"""
    baseline = SourceBaseline()
    for line in lines:
        baseline.observe_line(line)
    return baseline


def _is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _score_one_field(field_stat: _FieldStat, value: str) -> str | None:
    """单字段判定:没见过的值=新实体,见过但占比极低=罕见值;否则 None。
    纯数值度量(ms/rows/port/bytes 等延迟/计数)跳过——它们是度量不是实体,每个值本就不同,新值/罕见值无安全
    意义(数值类威胁如拖库行数、异常延迟靠正则规则/阈值抓,不靠新实体检测)。"""
    if _is_numeric(value):
        return None
    if value not in field_stat.values:
        return "新实体"
    if field_stat.values[value] / max(field_stat.total, 1) < _RARE_RATIO:
        return "罕见值"
    return None


def score_anomaly(baseline: SourceBaseline, raw_line: str, *, min_records: int = 100) -> tuple[float, list[str]]:
    """对一条记录评异常分数 + 原因。判据:低基数字段出现新实体 / 罕见值。
    基线样本不足 min_records 时返回 (0,[]) —— 冷启动只学不判,避免一开始把什么都当新。"""
    if baseline.records < min_records:
        return 0.0, []
    reasons: list[str] = []
    checked = 0
    for key, val in extract_entities(raw_line).items():
        field_stat = baseline.fields.get(key)
        if field_stat is None or field_stat.is_high_card():
            continue  # 无基线 or 高基数字段(时间戳/id)不做新实体检测
        checked += 1
        verdict = _score_one_field(field_stat, val)
        if verdict is not None:
            reasons.append(f"{key}={verdict}({val})")
    if not reasons or not checked:
        return 0.0, []
    score = min(1.0, 0.4 + len(reasons) / checked)  # 命中即给 0.4 基础分,按命中字段占比叠加
    return round(score, 3), reasons


_SAFE_SOURCE = re.compile(r"[^A-Za-z0-9._-]+")


def load_baseline(store: LogOpsStore, source_id: str) -> SourceBaseline:
    """读该源落盘基线(没有则空基线,从头学)。"""
    path = store.baseline_dir / f"{_SAFE_SOURCE.sub('-', source_id)}.json"
    return SourceBaseline.from_dict(read_json_object(path))


def save_baseline(store: LogOpsStore, source_id: str, model: SourceBaseline) -> None:
    """落盘该源基线(daemon 每拍学完持久化,跨重启续学)。"""
    store.baseline_dir.mkdir(parents=True, exist_ok=True)
    path = store.baseline_dir / f"{_SAFE_SOURCE.sub('-', source_id)}.json"
    write_json_file_atomic(path, model.to_dict())


__all__ = [
    "extract_entities",
    "SourceBaseline",
    "build_baseline",
    "score_anomaly",
    "load_baseline",
    "save_baseline",
]
