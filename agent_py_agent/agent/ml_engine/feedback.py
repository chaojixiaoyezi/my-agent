
from __future__ import annotations

"""②用户私有标注库 + 标注回流调融合权重(M1 的"会学")。

OutcomeLabel 写盘到 store.root 下 ml_labels.jsonl(append)——store.root 在 owner-scoped workspace,
天然 per-user 隔离(用户 A 的标注 B 看不到)。learn_weights 据标注反馈微调三路融合权重:agent 确认的
真威胁(success/attempt)多 → 提无监督权重(更敏感抓行为异常);判误报(failure/other)多 → 提监督权重
(更依赖规则严重度)。这是 co-teaming 闭环的 M1 落地:agent 复核结论回流,引擎下次评级随之调,不死板。

护栏:各路权重 clamp 到 [_MIN_WEIGHT,_MAX_WEIGHT] 再归一化,防一批异常标注把某路调爆/调零(权重漂移)。
"""

import json
from pathlib import Path

from .models import OutcomeLabel, SignalOutcome

_LABELS_FILENAME = "ml_labels.jsonl"
_LEARN_RATE = 0.05  # 每条真威胁/误报标注对权重的微调步长
_MIN_WEIGHT = 0.1
_MAX_WEIGHT = 0.8
_THREAT_OUTCOMES = {SignalOutcome.SUCCESS, SignalOutcome.ATTEMPT}


def labels_path(store_root: Path) -> Path:
    return Path(store_root) / _LABELS_FILENAME


def append_label(store_root: Path, label: OutcomeLabel) -> None:
    """追加一条标注到私有标注库(owner-scoped:store_root 在 owner workspace,跨用户不可见)。"""
    path = labels_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(label.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(blob)


def _parse_label(line: str) -> OutcomeLabel | None:
    text = line.strip()
    if not text:
        return None
    try:
        return OutcomeLabel.from_dict(json.loads(text))
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return None


def read_labels(store_root: Path) -> list[OutcomeLabel]:
    """读私有标注库;不存在/坏行→跳过。"""
    path = labels_path(store_root)
    if not path.exists():
        return []
    out: list[OutcomeLabel] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_label(line)
        if parsed is not None:
            out.append(parsed)
    return out


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """clamp 各项到 [min,max] 再归一化(和为 1),防自适应把某路调爆/调零(权重漂移护栏)。"""
    clamped = {k: min(_MAX_WEIGHT, max(_MIN_WEIGHT, v)) for k, v in weights.items()}
    total = sum(clamped.values()) or 1.0
    return {k: round(v / total, 4) for k, v in clamped.items()}


def learn_weights(base_weights: dict[str, float], labels: list[OutcomeLabel]) -> dict[str, float]:
    """据标注反馈微调三路融合权重(M1 会学)。真威胁多→提无监督(更敏感);误报多→提监督(靠规则)。

    无标注则原样返回 base(归一化)。带护栏:各项 clamp + 归一化。
    """
    if not labels:
        return _normalize_weights(dict(base_weights))
    threats = sum(1 for lb in labels if lb.outcome in _THREAT_OUTCOMES)
    benign = len(labels) - threats
    return _normalize_weights(
        {
            "supervised": base_weights.get("supervised", 0.5) + _LEARN_RATE * benign,
            "unsupervised": base_weights.get("unsupervised", 0.3) + _LEARN_RATE * threats,
            "correlation": base_weights.get("correlation", 0.2),
        }
    )


__all__ = ["labels_path", "append_label", "read_labels", "learn_weights"]
