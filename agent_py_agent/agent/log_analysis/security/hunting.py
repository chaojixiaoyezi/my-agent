# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Query-plan drafts for analyst and hunt agents."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import CaseRecord
from .correlation import RouteDraft


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 HuntQuery 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 HuntQuery 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class HuntQuery:
    query_id: str
    purpose: str
    seed_type: str
    seed_value: str
    source_products: list[str] = field(default_factory=list)
    time_window: tuple[str, str] = ("", "")
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 500

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 build_seed_hunt_queries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build seed hunt queries 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_seed_hunt_queries(
    seed_type: str,
    seed_value: str,
    *,
    start: str = "",
    end: str = "",
) -> list[HuntQuery]:
    clean_type = str(seed_type or "").strip()
    clean_value = str(seed_value or "").strip()
    if not clean_type or not clean_value:
        return []
    products_by_seed = {
        "ip": ["waf", "vpn", "edr", "dns", "proxy", "netflow"],
        "attacker_ip": ["waf", "vpn", "firewall", "proxy"],
        "victim_ip": ["edr", "hids", "netflow", "dns", "proxy"],
        "user": ["vpn", "sso", "ad", "windows", "pam"],
        "host": ["edr", "hids", "windows", "linux", "netflow"],
        "domain": ["dns", "proxy", "waf"],
        "hash": ["edr", "hids", "sandbox"],
        "process": ["edr", "sysmon", "linux_audit"],
    }
    products = products_by_seed.get(clean_type, ["waf", "vpn", "edr", "dns", "proxy"])
    specs = (
        ("Find direct sightings of the seed.", products),
        ("Find entities related to the seed before and after the case window.", products),
        ("Search peer assets or accounts for the same behavior.", products),
    )
    seed_digest = hashlib.sha256(f"{clean_type}:{clean_value}".encode()).hexdigest()[:8]
    return [
        HuntQuery(
            query_id=f"hunt-{clean_type}-{seed_digest}-{index + 1}",
            purpose=purpose,
            seed_type=clean_type,
            seed_value=clean_value,
            source_products=sources,
            time_window=(start, end),
            filters={clean_type: clean_value},
        )
        for index, (purpose, sources) in enumerate(specs)
    ]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 build_case_hunt_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build case hunt plan 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_case_hunt_plan(case: CaseRecord | Mapping[str, Any], route: RouteDraft | Mapping[str, Any] | None = None) -> list[HuntQuery]:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_dict = route.to_dict() if isinstance(route, RouteDraft) else dict(route or {})
    start, end = _case_window(case_obj, route_dict)
    queries: list[HuntQuery] = []
    for seed_type, values in case_obj.entities.items():
        if seed_type not in {"attacker_ip", "victim_ip", "src_ip", "dst_ip", "user", "host", "domain", "process"}:
            continue
        normalized_type = "ip" if seed_type in {"src_ip", "dst_ip"} else seed_type
        for value in values[:3]:
            queries.extend(build_seed_hunt_queries(normalized_type, value, start=start, end=end))
    return _dedupe_queries(queries)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 next_query_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 next query plan 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def next_query_plan(case: CaseRecord | Mapping[str, Any], route: RouteDraft | Mapping[str, Any] | None = None) -> dict[str, Any]:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_dict = route.to_dict() if isinstance(route, RouteDraft) else dict(route or {})
    return {
        "case_id": case_obj.case_id,
        "next_queries": _unique([*case_obj.next_queries, *route_dict.get("next_queries", [])]),
        "hunt_queries": [query.to_dict() for query in build_case_hunt_plan(case_obj, route_dict)],
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 retrohunt_query_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 retrohunt query plan 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def retrohunt_query_plan(seed_type: str, seed_value: str, *, days: int = 30) -> dict[str, Any]:
    return {
        "kind": "retrohunt",
        "seed_type": seed_type,
        "seed_value": seed_value,
        "lookback_days": days,
        "queries": [query.to_dict() for query in build_seed_hunt_queries(seed_type, seed_value)],
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _case_window 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 case window 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _case_window(case: CaseRecord, route: Mapping[str, Any]) -> tuple[str, str]:
    timeline = route.get("timeline") or []
    times = [str(item.get("time", "")) for item in timeline if isinstance(item, Mapping) and item.get("time")]
    if times:
        return (min(times), max(times))
    return (case.created_at, case.updated_at)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _dedupe_queries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe queries 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe_queries(queries: Sequence[HuntQuery]) -> list[HuntQuery]:
    result: list[HuntQuery] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for query in queries:
        key = (query.seed_type, query.seed_value, tuple(query.source_products))
        if key in seen:
            continue
        seen.add(key)
        result.append(query)
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _unique 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique 涉及的字段，让后续匹配和存储使用同一形态。
def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = [
    "HuntQuery",
    "build_case_hunt_plan",
    "build_seed_hunt_queries",
    "next_query_plan",
    "retrohunt_query_plan",
]
