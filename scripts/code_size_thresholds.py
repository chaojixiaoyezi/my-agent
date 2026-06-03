
from __future__ import annotations

"""Shared threshold helpers for the code-size checker."""

import math
from dataclasses import dataclass

from code_size_rules import NEAR_SOFT_RATIO, Finding


@dataclass(frozen=True)
class FindingInput:
    kind: str
    rel: str
    name: str
    value: int
    limit: int
    message: str


@dataclass(frozen=True)
class LimitFindingInput:
    base: FindingInput
    hard_limit: int


def near_soft_floor(soft_limit: int) -> int:
    return math.ceil(soft_limit * NEAR_SOFT_RATIO)


def is_near_soft(value: int, soft_limit: int) -> bool:
    return near_soft_floor(soft_limit) <= value <= soft_limit


def near_soft_finding(data: FindingInput) -> Finding:
    return Finding(
        data.kind,
        data.rel,
        data.name,
        data.value,
        data.limit,
        "high-risk",
        f"near soft limit: {data.message}",
    )


def limit_finding(data: LimitFindingInput) -> Finding:
    severity = "hard" if data.base.value > data.hard_limit else "soft"
    limit = data.hard_limit if severity == "hard" else data.base.limit
    return Finding(data.base.kind, data.base.rel, data.base.name, data.base.value, limit, severity, data.base.message)
