
from __future__ import annotations

"""检测规则的 owner-scoped 私有存储 —— agent 现编的声明式规则写盘,跨用户不可见。

store_root 在 owner workspace(agent.root/.log_ops/<monitor>),天然 per-user 隔离:用户 A 现编的检测规则
B 看不到。规则脱敏后(不含原始日志、是通用聚合声明)可沉淀进领域经验包跨用户复用(M2-3)。
"""

import json
from pathlib import Path

from .detection import DetectionRule, parse_rule

_RULES_FILENAME = "ml_rules.jsonl"


def rules_path(store_root: Path) -> Path:
    return Path(store_root) / _RULES_FILENAME


def append_rule(store_root: Path, rule: DetectionRule) -> None:
    """追加一条检测规则到私有规则库(owner-scoped)。"""
    path = rules_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rule.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def _parse_line(line: str) -> DetectionRule | None:
    text = line.strip()
    if not text:
        return None
    try:
        parsed = parse_rule(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, DetectionRule) else None


def read_rules(store_root: Path) -> list[DetectionRule]:
    """读私有规则库;坏行/非法规则跳过。返回 DetectionRule 列表。"""
    path = rules_path(store_root)
    if not path.exists():
        return []
    out: list[DetectionRule] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        rule = _parse_line(line)
        if rule is not None:
            out.append(rule)
    return out


__all__ = ["rules_path", "append_rule", "read_rules"]
