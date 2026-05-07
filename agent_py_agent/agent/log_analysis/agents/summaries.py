# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Compact summaries for parent-session and subagent handoff."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from .contracts import normalize_evidence_refs

CASE_FIELDS = (
    "case_id",
    "id",
    "title",
    "summary",
    "status",
    "priority",
    "severity",
    "risk_score",
    "confidence",
    "created_at",
    "updated_at",
    "time_window",
    "start_time",
    "end_time",
)

ROUTE_FIELDS = (
    "entry_candidates",
    "timeline",
    "impacted_entities",
    "lateral_movement",
    "gaps",
    "next_queries",
    "route_confidence",
)

EVIDENCE_REF_FIELDS = (
    "evidence_ref",
    "evidence_id",
    "ref",
    "id",
    "query_id",
    "path",
    "uri",
    "content_hash",
    "sha256",
    "source",
    "source_id",
    "summary",
    "row_count",
    "truncated",
)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 CaseSummary 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CaseSummary 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class CaseSummary:
    """Small summary safe for parent prompts."""

    case: dict[str, Any]
    evidence: list[dict[str, Any] | str] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)

    # LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {
            "case": dict(self.case),
            "evidence": list(self.evidence),
            "route": dict(self.route),
        }


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _get 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _to_mapping 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把 to mapping 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        return payload if isinstance(payload, Mapping) else {}
    if is_dataclass(value):
        return asdict(value)
    return {}


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _items 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 items 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _items(source: Any) -> list[Any]:
    if source is None:
        return []
    if isinstance(source, str) or _to_mapping(source):
        return [source]
    try:
        return list(source)
    except TypeError:
        return [source]


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _compact_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compact text 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _compact_text(value: Any, *, limit: int = 220) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _compact_list 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compact list 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _compact_list(value: Any, *, limit: int = 8) -> list[Any]:
    output: list[Any] = []
    for item in _items(value)[:limit]:
        _append_compact_item(output, item, limit=limit)
    return output


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _append_compact_item 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append compact item 相关记录，集中处理目标路径、格式化和状态更新。
def _append_compact_item(output: list[Any], item: Any, *, limit: int) -> None:
    compact = _compact_mapping(item, limit=limit) if isinstance(item, Mapping) else _compact_text(item)
    if compact not in (None, ""):
        output.append(compact)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _compact_mapping 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compact mapping 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _compact_mapping(value: Mapping[str, Any], *, limit: int = 8) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        if item in (None, "", [], {}):
            continue
        output[str(key)] = _compact_value(item, limit=limit)
    return output


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _compact_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compact value 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _compact_value(value: Any, *, limit: int = 8) -> Any:
    if isinstance(value, Mapping):
        return _compact_mapping(value, limit=limit)
    if isinstance(value, list):
        return _compact_list(value, limit=limit)
    return _compact_text(value)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _summarize_case_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summarize case fields 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _summarize_case_fields(case: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    case_id = _get(case, "case_id") or _get(case, "id")
    if case_id:
        output["case_id"] = _compact_text(case_id, limit=120)

    for field_name in CASE_FIELDS:
        if field_name in {"case_id", "id"}:
            continue
        value = _get(case, field_name)
        if value in (None, "", [], {}):
            continue
        output[field_name] = _compact_value(value)

    entity_refs = _get(case, "entity_refs") or _get(case, "entities")
    if entity_refs:
        output["entity_refs"] = _compact_list(entity_refs)
    finding_refs = _get(case, "finding_refs") or _get(case, "findings")
    if finding_refs:
        output["finding_refs"] = _compact_list(finding_refs)
    return output


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _summarize_evidence 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 summarize evidence 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _summarize_evidence(evidence: Any) -> list[dict[str, Any] | str]:
    output: list[dict[str, Any] | str] = []
    seen: set[str] = set()
    for item in _items(evidence)[:12]:
        _append_evidence_summary(output, seen, item)
    return output


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _append_evidence_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append evidence summary 相关记录，集中处理目标路径、格式化和状态更新。
def _append_evidence_summary(output: list[dict[str, Any] | str], seen: set[str], item: Any) -> None:
    mapping = _to_mapping(item)
    if mapping:
        _append_compact_evidence(output, seen, mapping)
        return
    _append_evidence_ref(output, seen, item)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _append_evidence_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append evidence ref 相关记录，集中处理目标路径、格式化和状态更新。
def _append_evidence_ref(output: list[dict[str, Any] | str], seen: set[str], item: Any) -> None:
    refs = normalize_evidence_refs(item, limit=1)
    if refs and refs[0] not in seen:
        output.append(refs[0])
        seen.add(refs[0])


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _append_compact_evidence 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append compact evidence 相关记录，集中处理目标路径、格式化和状态更新。
def _append_compact_evidence(output: list[dict[str, Any] | str], seen: set[str], mapping: Mapping[str, Any]) -> None:
    compact = _compact_evidence_mapping(mapping)
    ref_key = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if compact and ref_key not in seen:
        output.append(compact)
        seen.add(ref_key)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _compact_evidence_mapping 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 compact evidence mapping 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _compact_evidence_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        field_name: _compact_text(mapping.get(field_name))
        for field_name in EVIDENCE_REF_FIELDS
        if mapping.get(field_name) not in (None, "", [], {})
    }
    metadata = mapping.get("metadata")
    if isinstance(metadata, Mapping):
        _merge_evidence_metadata(compact, metadata)
    return compact


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _merge_evidence_metadata 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge evidence metadata 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_evidence_metadata(compact: dict[str, Any], metadata: Mapping[str, Any]) -> None:
    evidence_path = metadata.get("evidence_path") or metadata.get("path")
    if evidence_path and "path" not in compact:
        compact["path"] = _compact_text(evidence_path)
    sha256 = metadata.get("sha256") or metadata.get("content_hash")
    if sha256 and "sha256" not in compact and "content_hash" not in compact:
        compact["sha256"] = _compact_text(sha256)


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 _summarize_route 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summarize route 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def _summarize_route(route: Any) -> dict[str, Any]:
    if not route:
        return {}
    output: dict[str, Any] = {}
    for field_name in ROUTE_FIELDS:
        value = _get(route, field_name)
        if value in (None, "", [], {}):
            continue
        output[field_name] = _compact_value(value)
    return output


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 summarize_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summarize case 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def summarize_case(case: Any) -> CaseSummary:
    """Build a compact case/evidence/route summary.

    This function intentionally reads only allowlisted summary/ref fields. It
    does not inspect raw_events, raw payloads, query rows, or transcripts."""

    evidence = (
        _get(case, "evidence_refs")
        or _get(case, "evidence")
        or _get(case, "evidence_summary")
    )
    route = _get(case, "route_summary") or _get(case, "route_draft") or _get(case, "route")
    return CaseSummary(
        case=_summarize_case_fields(case),
        evidence=_summarize_evidence(evidence),
        route=_summarize_route(route),
    )


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 render_case_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 render case summary 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def render_case_summary(summary: CaseSummary | Mapping[str, Any]) -> str:
    payload = summary.to_dict() if isinstance(summary, CaseSummary) else dict(summary)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: agent 协作层定义日志分析 prompt、契约和总结结构；修改 case_summary_for_prompt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 case summary for prompt 在当前模块中的核心转换或协调步骤，衔接 agent 协作层定义日志分析 prompt、契约和总结结构。
def case_summary_for_prompt(case: Any) -> str:
    return render_case_summary(summarize_case(case))
