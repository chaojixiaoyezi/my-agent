
from __future__ import annotations

"""③领域经验包:agent 现编的检测规则 + 调好的融合权重 + 已知威胁指纹,脱敏沉淀成领域包,
跨子代理/用户/时间复用,新任务冷启动加载不重训。

钥匙(分离数据与经验):原始日志=隐私 per-user 隔离;学到的经验=脱敏可泛化→可复用。
- 检测规则:通用聚合声明(group_by/aggregate/threshold),不含原始日志/具体 IP → 天然脱敏。
- 融合权重:纯数字 → 脱敏。
- known_fingerprints:从标注 cluster_id 取规则指纹部分,剥离 domain 源标识 + entity:IP → 脱敏。

存储 experience_root/<domain>.json:默认 owner-scoped(同用户跨时间复用,无隐私问题);跨用户共享配
共享 experience_root + opt-in(隐私护栏)。merge:出厂内置 base + 前人沉淀 overlay(规则并集/权重取新/指纹并集)。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .detection import DetectionRule, parse_rule

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class DomainExperiencePack:
    """一个领域(如 web_api_logs)的脱敏经验包,跨用户迁移载体。"""

    domain_id: str
    version: int
    detection_rules: tuple[dict[str, Any], ...]   # 脱敏检测规则声明
    fusion_weights: dict[str, float]
    known_fingerprints: tuple[str, ...]           # 脱敏威胁指纹(规则名组合)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id, "version": self.version,
            "detection_rules": list(self.detection_rules), "fusion_weights": dict(self.fusion_weights),
            "known_fingerprints": list(self.known_fingerprints),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainExperiencePack":
        return cls(
            domain_id=str(data.get("domain_id") or ""),
            version=int(data.get("version") or 1),
            detection_rules=tuple(data.get("detection_rules") or []),
            fusion_weights=dict(data.get("fusion_weights") or {}),
            known_fingerprints=tuple(data.get("known_fingerprints") or []),
        )


def _fingerprint_part(cluster_id: str) -> str:
    """从 cluster_id(domain:fingerprint) 取规则指纹,剥离 domain 源标识;entity:IP → entity(脱敏)。"""
    parts = cluster_id.split(":", 1)
    tail = parts[1] if len(parts) > 1 else cluster_id
    return "entity" if tail.startswith("entity") else tail


def export_pack(domain_id: str, rules: list[DetectionRule], weights: dict[str, float], label_cluster_ids: list[str]) -> DomainExperiencePack:
    """从私有(规则/权重/标注 cluster_id)脱敏导出领域包。检测规则声明天然脱敏(无原始日志)。"""
    sanitized = tuple(rule.to_dict() for rule in rules)
    fingerprints = tuple(sorted({_fingerprint_part(cid) for cid in label_cluster_ids if cid}))
    return DomainExperiencePack(domain_id, 1, sanitized, dict(weights), fingerprints)


def merge_packs(base: DomainExperiencePack, overlay: DomainExperiencePack) -> DomainExperiencePack:
    """合并出厂 base + 沉淀 overlay:规则按 rule_id 去重并集,权重取 overlay(更新的),指纹并集,版本递增。"""
    rules_by_id = {r.get("rule_id"): r for r in base.detection_rules}
    for rule in overlay.detection_rules:
        rules_by_id[rule.get("rule_id")] = rule
    fingerprints = tuple(sorted(set(base.known_fingerprints) | set(overlay.known_fingerprints)))
    return DomainExperiencePack(
        base.domain_id,
        max(base.version, overlay.version) + 1,
        tuple(rules_by_id.values()),
        dict(overlay.fusion_weights or base.fusion_weights),
        fingerprints,
    )


def pack_path(experience_root: Path, domain_id: str) -> Path:
    return Path(experience_root) / f"{_SAFE.sub('-', domain_id) or 'default'}.json"


def save_pack(experience_root: Path, pack: DomainExperiencePack) -> None:
    """落盘领域包(若已有同 domain 包则 merge 后写,沉淀累积不覆盖)。"""
    existing = load_pack(experience_root, pack.domain_id)
    merged = merge_packs(existing, pack) if existing else pack
    path = pack_path(experience_root, pack.domain_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")


def load_pack(experience_root: Path, domain_id: str) -> DomainExperiencePack | None:
    """加载领域包;不存在/坏档返回 None。"""
    path = pack_path(experience_root, domain_id)
    if not path.exists():
        return None
    try:
        return DomainExperiencePack.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def rules_from_pack(pack: DomainExperiencePack) -> list[DetectionRule]:
    """领域包检测规则声明 → DetectionRule 列表(冷启动应用到私有规则库)。坏规则跳过。"""
    out: list[DetectionRule] = []
    for raw in pack.detection_rules:
        rule = parse_rule(raw) if isinstance(raw, dict) else "skip"
        if isinstance(rule, DetectionRule):
            out.append(rule)
    return out


__all__ = [
    "DomainExperiencePack",
    "export_pack",
    "merge_packs",
    "pack_path",
    "save_pack",
    "load_pack",
    "rules_from_pack",
]
