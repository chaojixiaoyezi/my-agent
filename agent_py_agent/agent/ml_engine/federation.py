
from __future__ import annotations

"""联邦聚合(M3-4)——多用户经验在共享层隐私保护聚合,跨用户复用不泄露数据。

M2-3 经验包是私有沉淀;M3-4 真正联邦:多用户贡献脱敏经验到共享领域包,聚合时隐私护栏:
- **k-匿名**:某规则签名/指纹 ≥k 个不同贡献者都有,才入共享(单用户独有的不入,避免反推出"是谁");
- **中位数聚合权重**:抗异常值/投毒(单个恶意贡献拉不动中位数,不像均值会被极端值带偏);
- 贡献本身脱敏(规则签名不含 rule_id/name;指纹已脱敏)。
opt-in:用户显式授权才贡献(隐私优先,见工具层)。共享路径由部署配(跨用户可达),默认不启用。
"""

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .experience import DomainExperiencePack

_CONTRIB_FILENAME = "federation_contributions.jsonl"
_WEIGHT_KEYS = ("supervised", "unsupervised", "correlation")


@dataclass(frozen=True)
class Contribution:
    """一个(匿名)用户对某领域的脱敏贡献,来自其私有经验包。"""

    contributor_id: str  # 匿名化标识(hash),只用于 k-匿名计数,不可追溯到人
    domain_id: str
    detection_rules: tuple[dict[str, Any], ...]
    fusion_weights: dict[str, float]
    known_fingerprints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"contributor_id": self.contributor_id, "domain_id": self.domain_id,
                "detection_rules": list(self.detection_rules), "fusion_weights": dict(self.fusion_weights),
                "known_fingerprints": list(self.known_fingerprints)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contribution":
        return cls(str(data.get("contributor_id") or ""), str(data.get("domain_id") or ""),
                   tuple(data.get("detection_rules") or []), dict(data.get("fusion_weights") or {}),
                   tuple(data.get("known_fingerprints") or []))


def contribution_from_pack(contributor_id: str, pack: DomainExperiencePack) -> Contribution:
    """从用户私有经验包构造一个脱敏联邦贡献(contributor_id 匿名)。"""
    return Contribution(contributor_id, pack.domain_id, pack.detection_rules, pack.fusion_weights, pack.known_fingerprints)


def _rule_signature(rule: dict[str, Any]) -> str:
    """规则脱敏签名(跨贡献者去重统计):group_by+aggregate+字段+判据,**不含 rule_id/name**(更不可追溯)。"""
    return f"{sorted(rule.get('group_by') or [])}|{rule.get('aggregate')}|{rule.get('agg_field')}|{bool(rule.get('baseline_deviation'))}"


def _contrib_rules_by_sig(contrib: Contribution) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rule in contrib.detection_rules:
        out.setdefault(_rule_signature(rule), rule)
    return out


def _k_anon_rules(contributions: list[Contribution], k: int) -> list[dict[str, Any]]:
    """≥k 个**独立贡献者**都有的规则签名才入共享(k-匿名,避免单用户独有规则被反推)。
    必须按 contributor_id 去重计数(与 _k_anon_fingerprints 一致):否则同一贡献者的 Contribution
    在列表里重复出现时会被当成多个贡献者、绕过 k-匿名、泄露单贡献者私有规则。"""
    rule_by_sig: dict[str, dict[str, Any]] = {}
    contributors_by_sig: dict[str, set[str]] = defaultdict(set)
    for contrib in contributions:
        for sig, rule in _contrib_rules_by_sig(contrib).items():
            rule_by_sig.setdefault(sig, rule)
            contributors_by_sig[sig].add(contrib.contributor_id)
    return [rule_by_sig[sig] for sig in rule_by_sig if len(contributors_by_sig[sig]) >= k]


def _median_weights(contributions: list[Contribution]) -> dict[str, float]:
    """各路权重取中位数(抗异常/投毒:单个恶意贡献拉不动中位数)。"""
    out: dict[str, float] = {}
    for key in _WEIGHT_KEYS:
        vals = [c.fusion_weights.get(key, 0.0) for c in contributions if key in c.fusion_weights]
        out[key] = round(statistics.median(vals), 4) if vals else 0.0
    return out


def _k_anon_fingerprints(contributions: list[Contribution], k: int) -> tuple[str, ...]:
    counts: dict[str, set[str]] = defaultdict(set)
    for contrib in contributions:
        for fingerprint in contrib.known_fingerprints:
            counts[fingerprint].add(contrib.contributor_id)
    return tuple(sorted(fp for fp, who in counts.items() if len(who) >= k))


def aggregate_contributions(contributions: list[Contribution], k_anonymity: int = 2) -> DomainExperiencePack:
    """联邦聚合多用户贡献成共享领域包:k-匿名规则/指纹(≥k 贡献者才入)+ 中位数权重(抗投毒)。"""
    if not contributions:
        return DomainExperiencePack("", 1, (), {}, ())
    return DomainExperiencePack(
        contributions[0].domain_id, 1,
        tuple(_k_anon_rules(contributions, k_anonymity)),
        _median_weights(contributions),
        _k_anon_fingerprints(contributions, k_anonymity),
    )


def contributions_path(federation_root: Path) -> Path:
    return Path(federation_root) / _CONTRIB_FILENAME


def append_contribution(federation_root: Path, contrib: Contribution) -> None:
    """追加一条贡献到共享联邦层(跨用户共享路径)。"""
    path = contributions_path(federation_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(contrib.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _parse_contribution(line: str) -> Contribution | None:
    text = line.strip()
    if not text:
        return None
    try:
        return Contribution.from_dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def read_contributions(federation_root: Path, domain_id: str = "") -> list[Contribution]:
    """读共享层贡献(domain_id 非空时只取该领域);坏行跳过。"""
    path = contributions_path(federation_root)
    if not path.exists():
        return []
    out: list[Contribution] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        contrib = _parse_contribution(line)
        if contrib is not None and (not domain_id or contrib.domain_id == domain_id):
            out.append(contrib)
    return out


__all__ = [
    "Contribution",
    "contribution_from_pack",
    "aggregate_contributions",
    "contributions_path",
    "append_contribution",
    "read_contributions",
]
