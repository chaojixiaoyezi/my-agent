
from __future__ import annotations

"""rule file loading and caching for log analysis detectors.

新手说明:
这个文件放的是规则加载和缓存逻辑。
规则数据从 security_rules 模块获取，这里处理加载和缓存。
"""

from typing import Any

from ..security_rules import get_rule

# Cache for loaded rules
_RULE_CACHE: dict[str, Any] = {}


def load_rule(detector_id: str) -> Any:
    """load a single rule by detector ID with caching.

    新手说明:
    从 security_rules 加载规则，使用缓存避免重复加载。

    `detector_id` 是检测器 ID。

    返回说明:
    返回规则对象。"""

    if detector_id not in _RULE_CACHE:
        _RULE_CACHE[detector_id] = get_rule(detector_id)
    return _RULE_CACHE[detector_id]


def clear_rule_cache() -> None:
    """clear the rule cache."""
    _RULE_CACHE.clear()


def preload_rules(detector_ids: list[str]) -> None:
    """preload multiple rules into cache.

    新手说明:
    预先加载多个规则到缓存中，提高后续访问速度。

    `detector_ids` 是要预加载的检测器 ID 列表。"""

    for detector_id in detector_ids:
        load_rule(detector_id)
