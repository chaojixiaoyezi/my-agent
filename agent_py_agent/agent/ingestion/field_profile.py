"""字段画像:每个字段路径的纯结构化取值统计(基数/单调性/数量级),驱动签名分类。

分类只用结构信号:布尔与 None 恒取字面;低基数取字面;高基数字符串只留类型;
高基数数值看单调性(单调=游标/时间戳类,归并为一个记号)否则按数量级分桶。
高基数判定是粘性的(一旦超限不回退),保证签名跨批稳定。
"""

from __future__ import annotations

import math
from typing import Any

_LITERAL_TEXT_CAP = 48
_MONOTONE_MIN_SAMPLES = 16

TOKEN_HIGH_CARD_TEXT = "s:*"
TOKEN_MONOTONE_NUMBER = "n:mono"


class FieldProfile:
    """单个字段路径的滚动画像。observe() 先记账,token() 再按当前画像给出分类记号。"""

    __slots__ = ("count", "values", "overflowed", "numeric_count", "monotone_breaks", "last_number")

    def __init__(self) -> None:
        self.count = 0
        self.values: dict[str, int] = {}
        self.overflowed = False
        self.numeric_count = 0
        self.monotone_breaks = 0
        self.last_number: float | None = None

    def observe(self, value: object, limit: int) -> None:
        self.count += 1
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            self._observe_number(float(value))
        if not self.overflowed:
            self._observe_literal(value, limit)

    def _observe_number(self, number: float) -> None:
        self.numeric_count += 1
        if self.last_number is not None and number < self.last_number:
            self.monotone_breaks += 1
        self.last_number = number

    def _observe_literal(self, value: object, limit: int) -> None:
        key = _literal_key(value)
        if key in self.values:
            self.values[key] += 1
            return
        if len(self.values) >= limit:
            self.overflowed = True
            self.values = {}
            return
        self.values[key] = 1

    def token(self, value: object) -> str:
        """当前画像下该值的结构化记号(签名的组成单元)。"""
        if isinstance(value, bool):
            return "b:T" if value else "b:F"
        if value is None:
            return "null"
        if isinstance(value, (int, float)):
            return self._number_token(value)
        if isinstance(value, str):
            return _literal_key(value) if not self.overflowed else TOKEN_HIGH_CARD_TEXT
        return f"t:{type(value).__name__}"

    def _number_token(self, value: float) -> str:
        if isinstance(value, int) and not self.overflowed:
            return _literal_key(value)
        if self._is_monotone():
            return TOKEN_MONOTONE_NUMBER
        return f"n:e{_magnitude(value)}"

    def _is_monotone(self) -> bool:
        return self.numeric_count >= _MONOTONE_MIN_SAMPLES and self.monotone_breaks == 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "values": dict(self.values),
            "overflowed": self.overflowed,
            "numeric_count": self.numeric_count,
            "monotone_breaks": self.monotone_breaks,
            "last_number": self.last_number,
        }

    @classmethod
    def restore(cls, payload: dict[str, Any]) -> "FieldProfile":
        profile = cls()
        profile.count = int(payload.get("count") or 0)
        profile.values = {str(k): int(v) for k, v in dict(payload.get("values") or {}).items()}
        profile.overflowed = bool(payload.get("overflowed"))
        profile.numeric_count = int(payload.get("numeric_count") or 0)
        profile.monotone_breaks = int(payload.get("monotone_breaks") or 0)
        raw_last = payload.get("last_number")
        profile.last_number = float(raw_last) if raw_last is not None else None
        return profile


class ProfileTable:
    """路径 → FieldProfile 的表;observe_and_token 一步完成记账+取记号。"""

    __slots__ = ("profiles", "low_cardinality_limit")

    def __init__(self, low_cardinality_limit: int) -> None:
        self.profiles: dict[str, FieldProfile] = {}
        self.low_cardinality_limit = low_cardinality_limit

    def observe_and_token(self, path: str, value: object) -> str:
        profile = self._ensure(path)
        profile.observe(value, self.low_cardinality_limit)
        return profile.token(value)

    def observe_only(self, path: str, value: object) -> None:
        """冷启动预热:只喂画像不取记号(先让分类收敛,再统一取签名)。"""
        self._ensure(path).observe(value, self.low_cardinality_limit)

    def token_of(self, path: str, value: object) -> str:
        return self._ensure(path).token(value)

    def _ensure(self, path: str) -> FieldProfile:
        profile = self.profiles.get(path)
        if profile is None:
            profile = FieldProfile()
            self.profiles[path] = profile
        return profile

    def snapshot(self) -> dict[str, Any]:
        return {path: profile.snapshot() for path, profile in self.profiles.items()}

    def restore(self, payload: dict[str, Any]) -> None:
        for path, item in dict(payload or {}).items():
            if isinstance(item, dict):
                self.profiles[str(path)] = FieldProfile.restore(item)


def _literal_key(value: object) -> str:
    if isinstance(value, str):
        text = value if len(value) <= _LITERAL_TEXT_CAP else f"{value[:_LITERAL_TEXT_CAP]}…"
        return f"s:{text}"
    if isinstance(value, float):
        return f"n:{value!r}"
    return f"n:{value}"


def _magnitude(value: float) -> int:
    return int(math.floor(math.log10(abs(value) + 1.0)))


def is_literal_value_token(token: str) -> bool:
    """字面取值记号才代表"具体取值"(参与少数派统计/特征键):b:T/b:F、非折叠字面
    (s:xxx/n:123)、null。折叠/归并记号(s:*、n:mono、n:eX 数量级桶、t:类型)不算。"""
    if token in (TOKEN_HIGH_CARD_TEXT, TOKEN_MONOTONE_NUMBER):
        return False
    if token.startswith("t:"):
        return False
    if token.startswith("n:e") and token[3:].lstrip("-").isdigit():
        return False
    return True


__all__ = [
    "FieldProfile",
    "ProfileTable",
    "TOKEN_HIGH_CARD_TEXT",
    "TOKEN_MONOTONE_NUMBER",
    "is_literal_value_token",
]
