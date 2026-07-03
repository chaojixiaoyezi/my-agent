"""事件签名:压平后的 (路径, 记号) 对 → 稳定短哈希。同形状+同枚举态的事件共签名。"""

from __future__ import annotations

from hashlib import sha1

from .field_profile import ProfileTable
from .flatten import flatten_event

_SKETCH_FIELD_CAP = 14


def classed_pairs(event: object, profiles: ProfileTable) -> tuple[tuple[str, str], ...]:
    """压平事件并逐字段记账+取结构化记号;按路径排序保证签名稳定。"""
    return observed_pairs(flatten_event(event), profiles)


def observed_pairs(flat: list[tuple[str, object]], profiles: ProfileTable) -> tuple[tuple[str, str], ...]:
    """对已压平(可能已按 spec 滤掉忽略字段)的对做记账+取记号。"""
    return tuple(sorted((path, profiles.observe_and_token(path, value)) for path, value in flat))


def token_pairs_of(flat: list[tuple[str, object]], profiles: ProfileTable) -> tuple[tuple[str, str], ...]:
    """只取记号不记账(配合冷启动预热:画像已在预热遍喂过)。"""
    return tuple(sorted((path, profiles.token_of(path, value)) for path, value in flat))


def signature_of(pairs: tuple[tuple[str, str], ...]) -> str:
    joined = "\x1f".join(f"{path}\x1e{token}" for path, token in pairs)
    return sha1(joined.encode("utf-8")).hexdigest()[:12]


def sketch_of(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """给被压组看的结构速写:全部分类记号,超上限截断(纯投影,不做取舍判断)。"""
    sketch = dict(pairs[:_SKETCH_FIELD_CAP])
    if len(pairs) > _SKETCH_FIELD_CAP:
        sketch["…"] = f"+{len(pairs) - _SKETCH_FIELD_CAP} fields"
    return sketch


__all__ = ["classed_pairs", "observed_pairs", "signature_of", "sketch_of", "token_pairs_of"]
