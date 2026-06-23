
from __future__ import annotations

"""规则自适应优化(M3-2):agent 现编的检测规则越用越准。

co-teaming 闭环延伸到规则层:agent 标注某规则命中是误报还是真实 → 统计每规则误报率 → 建议
(误报率高=阈值太松→升阈值产新版本)。规则版本化(version 递增,保留迭代轨迹)。反馈私有(owner 隔离)。
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .detection import DetectionRule

_FEEDBACK_FILENAME = "ml_rule_feedback.jsonl"
_MIN_FEEDBACK = 5       # 反馈够这么多才给调整建议(否则样本太少不可信)
_HIGH_FP_RATE = 0.5     # 误报率 ≥ 此 = 阈值太松,建议升
_REVIEW_FP_RATE = 0.2   # 误报率 ≥ 此 = 建议人工复核
_THRESHOLD_BUMP = 1.5   # 升阈值倍数


@dataclass(frozen=True)
class RuleFeedback:
    """agent 对某规则某命中的标注:是误报还是真实威胁。"""

    rule_id: str
    group_key: str
    false_positive: bool
    noted_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "group_key": self.group_key,
                "false_positive": self.false_positive, "noted_at": self.noted_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleFeedback":
        return cls(str(data.get("rule_id") or ""), str(data.get("group_key") or ""),
                   bool(data.get("false_positive")), float(data.get("noted_at") or 0.0))


@dataclass(frozen=True)
class RuleEffectiveness:
    """某规则的效果评估:误报率 + 建议动作。"""

    rule_id: str
    total_feedback: int
    false_positives: int
    false_positive_rate: float
    suggested_action: str  # collecting | ok | review | raise_threshold


def _suggest(rate: float, total: int) -> str:
    if total < _MIN_FEEDBACK:
        return "collecting"
    if rate >= _HIGH_FP_RATE:
        return "raise_threshold"
    if rate >= _REVIEW_FP_RATE:
        return "review"
    return "ok"


def assess_rule(rule: DetectionRule, feedbacks: list[RuleFeedback]) -> RuleEffectiveness:
    """统计某规则误报率 + 建议(误报率高→升阈值)。反馈不足返回 collecting。"""
    relevant = [f for f in feedbacks if f.rule_id == rule.rule_id]
    fp = sum(1 for f in relevant if f.false_positive)
    total = len(relevant)
    rate = round(fp / total, 3) if total else 0.0
    return RuleEffectiveness(rule.rule_id, total, fp, rate, _suggest(rate, total))


def tune_rule(rule: DetectionRule, effectiveness: RuleEffectiveness) -> DetectionRule:
    """据评估自调:误报率高→阈值×1.5 + version+1 产新版本;否则原样返回。"""
    if effectiveness.suggested_action != "raise_threshold":
        return rule
    return replace(rule, threshold=round(rule.threshold * _THRESHOLD_BUMP, 3), version=rule.version + 1)


def feedback_path(store_root: Path) -> Path:
    return Path(store_root) / _FEEDBACK_FILENAME


def append_feedback(store_root: Path, feedback: RuleFeedback) -> None:
    """追加一条规则反馈(owner-scoped 私有)。"""
    path = feedback_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(feedback.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _parse_feedback(line: str) -> RuleFeedback | None:
    text = line.strip()
    if not text:
        return None
    try:
        return RuleFeedback.from_dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def read_feedbacks(store_root: Path) -> list[RuleFeedback]:
    """读规则反馈库;坏行跳过。"""
    path = feedback_path(store_root)
    if not path.exists():
        return []
    out: list[RuleFeedback] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        feedback = _parse_feedback(line)
        if feedback is not None:
            out.append(feedback)
    return out


__all__ = [
    "RuleFeedback",
    "RuleEffectiveness",
    "assess_rule",
    "tune_rule",
    "feedback_path",
    "append_feedback",
    "read_feedbacks",
]
