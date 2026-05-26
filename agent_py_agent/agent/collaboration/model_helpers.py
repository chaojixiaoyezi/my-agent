# LLM: Collaboration model helper functions; keep parsing permissive and open-world.
# 模块用途: 提供协作模型 ID 生成和宽松类型规整，避免模型定义文件继续膨胀。

from __future__ import annotations

from typing import Any

from ..conversation.models import new_id


def new_case_id() -> str:
    return new_id("case")


def new_request_id() -> str:
    return new_id("creq")


def new_evidence_id() -> str:
    return new_id("ev")


def new_decision_id() -> str:
    return new_id("cdec")


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item or "").strip())
    if value is None:
        return ()
    text = str(value).strip()
    return (text,) if text else ()


def _tuple_of_dicts(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
