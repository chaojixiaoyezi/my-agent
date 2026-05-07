# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

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


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 load_rule 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 load rule 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def load_rule(detector_id: str) -> Any:
    """load a single rule by detector ID with caching.

    新手说明:
    从 security_rules 加载规则，使用缓存避免重复加载。

    参数说明:
    `detector_id` 是检测器 ID。

    返回说明:
    返回规则对象。"""

    if detector_id not in _RULE_CACHE:
        _RULE_CACHE[detector_id] = get_rule(detector_id)
    return _RULE_CACHE[detector_id]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 clear_rule_cache 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 clear rule cache 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def clear_rule_cache() -> None:
    """clear the rule cache."""
    _RULE_CACHE.clear()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 preload_rules 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 preload rules 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def preload_rules(detector_ids: list[str]) -> None:
    """preload multiple rules into cache.

    新手说明:
    预先加载多个规则到缓存中，提高后续访问速度。

    参数说明:
    `detector_ids` 是要预加载的检测器 ID 列表。"""

    for detector_id in detector_ids:
        load_rule(detector_id)
