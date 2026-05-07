# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Pure helper functions for finding-to-case normalization."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding, QueryPlan


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _ensure_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure finding 的输入、状态或路径，提前暴露无效数据和越界条件。
def _ensure_finding(value: Finding | Mapping[str, Any]) -> Finding:
    return value if isinstance(value, Finding) else Finding.from_dict(value)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _ensure_case 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 ensure case 的输入、状态或路径，提前暴露无效数据和越界条件。
def _ensure_case(value: CaseRecord | Mapping[str, Any]) -> CaseRecord:
    return value if isinstance(value, CaseRecord) else CaseRecord.from_dict(value)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _title_for_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 title for finding 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _title_for_finding(finding: Finding) -> str:
    victim = _primary_victim(finding) or "unknown asset"
    return f"{finding.detector_id}: {victim}"


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _primary_attacker 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 primary attacker 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _primary_attacker(finding: Finding) -> str:
    values = _entity_values(finding.entities, "attacker_ip")
    return values[0] if values else ""


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _primary_victim 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 primary victim 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _primary_victim(finding: Finding) -> str:
    values = _entity_values(finding.entities, "victim_ip", "dst_ip", "host", "asset_id")
    if values:
        return values[0]
    source_values = _entity_values(finding.entities, "src_ip")
    return source_values[0] if source_values and not _primary_attacker(finding) else ""


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _primary_account 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 primary account 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _primary_account(finding: Finding) -> str:
    values = _entity_values(finding.entities, "user", "account", "principal")
    return values[0] if values else ""


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _entity_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 entity values 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _entity_values(entities: Mapping[str, Sequence[Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        _append_unique_texts(values, entities.get(key, []))
    return values


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _append_unique_texts 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append unique texts 相关记录，集中处理目标路径、格式化和状态更新。
def _append_unique_texts(output: list[str], values: Sequence[Any]) -> None:
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _merge_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge entities 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_entities(left: Mapping[str, Sequence[Any]], right: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {key: [str(value) for value in values] for key, values in left.items()}
    for key, values in right.items():
        merged.setdefault(key, [])
        merged[key].extend(str(value) for value in values)
    return _normalize_entities(merged)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _normalize_entities 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize entities 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize_entities(entities: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        normalized[clean_key] = _unique(str(value).strip() for value in values if str(value or "").strip())
    return {key: values for key, values in sorted(normalized.items()) if values}


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _merge_evidence_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge evidence refs 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_evidence_refs(left: Sequence[EvidenceRef], right: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    merged: dict[str, EvidenceRef] = {}
    for ref in [*left, *right]:
        evidence = ref if isinstance(ref, EvidenceRef) else EvidenceRef.from_dict(ref)
        merged[evidence.evidence_id] = evidence
    return list(merged.values())


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _case_type_for_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 case type for findings 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _case_type_for_findings(findings: Sequence[Finding]) -> str:
    detector_ids = {finding.detector_id for finding in findings}
    if {"waf_attack_success_candidate", "web_to_process_anomaly", "rare_egress_after_alert"}.issubset(detector_ids):
        return "suspected_zero_day_intrusion"
    if "vpn_new_geo_login" in detector_ids or "bruteforce_then_success" in detector_ids:
        return "suspected_credential_intrusion"
    if "waf_attack_success_candidate" in detector_ids:
        return "web_intrusion_candidate"
    return "security_investigation"


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _unique 时同步检查返回值、异常处理和读写副作用。
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


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _unique_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique values 涉及的字段，让后续匹配和存储使用同一形态。
def _unique_values(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item = _unique_marker_value(value)
        if not item:
            continue
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _unique_marker_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique marker value 涉及的字段，让后续匹配和存储使用同一形态。
def _unique_marker_value(value: Any) -> Any:
    if isinstance(value, QueryPlan):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return str(value or "").strip()


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _time_bucket 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 time bucket 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _time_bucket(value: str, minutes: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unknown-time"
    minute = (parsed.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = parsed.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return bucket.isoformat().replace("+00:00", "Z")


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _bucket_from_dedup_key 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bucket from dedup key 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def _bucket_from_dedup_key(value: str) -> str:
    for part in value.split("|"):
        if part.startswith("bucket="):
            return part.removeprefix("bucket=")
    return ""


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _parse_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 从外部数据还原 parse time 需要的领域对象，统一缺省值和兼容字段。
def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _stable_case_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 stable case id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _stable_case_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"case-{today}-{digest[:10]}"


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _clamp_score 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 clamp score 的判定结果，避免把推测当作事实写入。
def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))
