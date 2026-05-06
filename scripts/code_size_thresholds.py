from __future__ import annotations

"""Shared threshold helpers for the code-size checker."""

import math

from code_size_rules import NEAR_SOFT_RATIO, Finding


def near_soft_floor(soft_limit: int) -> int:
    return math.ceil(soft_limit * NEAR_SOFT_RATIO)


def is_near_soft(value: int, soft_limit: int) -> bool:
    return near_soft_floor(soft_limit) <= value <= soft_limit


def near_soft_finding(
    kind: str,
    rel: str,
    name: str,
    value: int,
    soft_limit: int,
    message: str,
) -> Finding:
    return Finding(
        kind,
        rel,
        name,
        value,
        soft_limit,
        "high-risk",
        f"near soft limit: {message}",
    )


def limit_finding(
    kind: str,
    rel: str,
    name: str,
    value: int,
    limits: tuple[int, int],
    message: str,
) -> Finding:
    soft_limit, hard_limit = limits
    severity = "hard" if value > hard_limit else "soft"
    limit = hard_limit if severity == "hard" else soft_limit
    return Finding(kind, rel, name, value, limit, severity, message)
