# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Formatting helpers for log-analysis report renderers."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import EvidenceRef, QueryPlan


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_facts 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet facts 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_facts(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No confirmed facts beyond the case shell are available yet."]
    return [
        f"- {item.get('statement', 'Observed fact')} Evidence: {', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_inferences 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet inferences 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_inferences(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No inferences available yet."]
    return [
        f"- {item.get('hypothesis', 'Hypothesis pending')} Confidence: {float(item.get('confidence', 0.0)):.2f}"
        for item in items
    ]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_entries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet entries 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_entries(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No entry candidate is ranked yet."]
    return [
        f"- {item.get('kind', 'candidate')} via {item.get('detector_id', 'unknown')} "
        f"confidence={float(item.get('confidence', 0.0)):.2f} evidence={', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_timeline 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 bullet timeline 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def bullet_timeline(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No timeline steps are available yet."]
    return [
        f"- {item.get('time', 'unknown time')}: {item.get('stage', 'unknown')} - {item.get('action', '')}"
        for item in items
    ]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet entities 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_entities(entities: Mapping[str, Sequence[Any]]) -> list[str]:
    if not entities:
        return ["- No impacted entities are available yet."]
    return [f"- {key}: {', '.join(str(value) for value in values)}" for key, values in sorted(entities.items()) if values]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_lateral 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet lateral 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_lateral(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No lateral movement sign is identified yet."]
    return [f"- {item.get('kind', 'lateral_candidate')}: {item.get('summary', '')}" for item in items]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet text 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_text(items: Sequence[Any]) -> list[str]:
    values = unique(items)
    return [f"- {item}" for item in values] if values else ["- None recorded."]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 bullet_query_plans 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bullet query plans 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def bullet_query_plans(items: Sequence[Any]) -> list[str]:
    values = unique_items(items)
    if not values:
        return ["- None recorded."]
    return [f"- {format_query_plan(item)}" for item in values]


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 format_query_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 format query plan 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def format_query_plan(item: Any) -> str:
    if not isinstance(item, Mapping):
        return str(item)
    display = str(item.get("display") or item.get("purpose") or "Query plan").strip()
    details = query_plan_details(item)
    return f"{display} ({'; '.join(details)})" if details else display


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 query_plan_details 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query plan details 的候选结果，并按参数完成筛选、排序或数量限制。
def query_plan_details(item: Mapping[str, Any]) -> list[str]:
    details: list[str] = []
    source_products = item.get("source_products") or []
    if source_products:
        details.append(f"sources={', '.join(str(value) for value in source_products)}")
    start_time = str(item.get("start_time") or "").strip()
    end_time = str(item.get("end_time") or "").strip()
    if start_time or end_time:
        details.append(f"time={start_time or '*'}..{end_time or '*'}")
    filters = item.get("filters")
    if isinstance(filters, Mapping) and filters:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(filters.items()) if value not in (None, "", [], {}))
        if rendered:
            details.append(f"filters: {rendered}")
    evidence_needed = item.get("evidence_needed") or []
    if evidence_needed:
        details.append(f"evidence={', '.join(str(value) for value in evidence_needed)}")
    limit = item.get("limit")
    if limit:
        details.append(f"limit={limit}")
    return details


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 raw_like_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 raw like refs 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def raw_like_refs(refs: Sequence[Any], ref_ids: Sequence[str]) -> list[str]:
    raw: list[str] = []
    for ref in refs:
        _append_raw_ref(raw, ref)
    raw.extend(ref for ref in ref_ids if ref.startswith("raw") or ":line-" in ref)
    return unique(raw)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _append_raw_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append raw ref 相关记录，集中处理目标路径、格式化和状态更新。
def _append_raw_ref(raw: list[str], ref: Any) -> None:
    if isinstance(ref, EvidenceRef) and ref.raw_ref:
        raw.append(ref.raw_ref)
        return
    if isinstance(ref, Mapping) and ref.get("raw_ref"):
        raw.append(str(ref.get("raw_ref")))


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ref_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref ids 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        result.append(ref_id(ref))
    return unique(result)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ref_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def ref_id(ref: Any) -> str:
    if isinstance(ref, EvidenceRef):
        return ref.evidence_id
    if isinstance(ref, Mapping):
        return str(ref.get("evidence_id") or ref.get("raw_ref") or ref)
    return str(ref)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 unique 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique 涉及的字段，让后续匹配和存储使用同一形态。
def unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 unique_items 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique items 涉及的字段，让后续匹配和存储使用同一形态。
def unique_items(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item: Any = value.to_dict() if isinstance(value, QueryPlan) else value
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen or item in ("", None, [], {}):
            continue
        seen.add(marker)
        result.append(item)
    return result
