from __future__ import annotations

"""Subagent coverage and requirement tracking records."""

from collections.abc import Callable
from typing import Any

from ..common.value_parsing import text_or_sequence_strings


# LLM: 从响应 payload 中提取结构化覆盖记录。
# 函数用途: 解析 payload 中的 coverage 记录。
def coverage_records_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    records = [
        *normalize_coverage_records(payload.get("coverage_records")),
        *normalize_coverage_records(payload.get("coverage")),
    ]
    top_level_coverer = _clean_id(payload.get("covered_by_run_id") or payload.get("covering_run_id"))
    records.extend(_records_from_covered_ids(payload.get("covered_run_ids"), top_level_coverer))
    return _dedupe_records(records)


# LLM: 标准化覆盖记录列表，过滤无效字典。
# 函数用途: 规范化覆盖记录结构。
def normalize_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        record = _coverage_record_from_item(item)
        if record:
            records.append(record)
    return _dedupe_records(records)


# LLM: 将新覆盖记录合并入任务属性并去重。
# 函数用途: 更新任务对象的 coverage_records 属性。
def merge_task_coverage_records(task: object, records: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = normalize_coverage_records(records)
    if not normalized:
        return task_coverage_records(task)
    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        attrs = {}
        task.attributes = attrs
    merged = _dedupe_records([*normalize_coverage_records(attrs.get("coverage_records")), *normalized])
    attrs["coverage_records"] = merged
    return merged


# LLM: 读取任务对象的结构化覆盖记录。
# 函数用途: 获取任务已登记的覆盖记录。
def task_coverage_records(task: object) -> list[dict[str, object]]:
    attrs = _attributes_dict(task)
    return normalize_coverage_records(attrs.get("coverage_records"))


# LLM: 批量获取多个任务的覆盖记录并全局去重。
# 函数用途: 汇总多个任务的覆盖记录。
def all_task_coverage_records(tasks: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for task in tasks:
        records.extend(task_coverage_records(task))
    return _dedupe_records(records)


# LLM: 判断目标 run_id 是否已被已验证的代理有效覆盖。
# 函数用途: 校验某子代理是否由验证通过的代理接替。
def coverage_records_resolve_run(
    run_id: str,
    records: list[dict[str, object]],
    is_verified: Callable[[str], bool],
) -> bool:
    target = _clean_id(run_id)
    if not target:
        return False
    return any(_record_resolves_run(target, record, is_verified) for record in records)


# LLM: 从单个字典结构提取覆盖记录，兼容 requirement_id 字段。
# 函数用途: 解析单条覆盖记录。
def _coverage_record_from_item(item: dict[str, object]) -> dict[str, object]:
    covered = _clean_id(item.get("covered_run_id") or item.get("source_run_id") or item.get("run_id"))
    coverer = _clean_id(
        item.get("covered_by_run_id")
        or item.get("covering_run_id")
        or item.get("replacement_run_id")
        or item.get("takeover_by")
    )
    if not covered or not coverer:
        return {}
    record: dict[str, object] = {
        "covered_run_id": covered,
        "covered_by_run_id": coverer,
        "reason": str(item.get("reason") or item.get("summary") or "").strip(),
        "artifact_refs": text_or_sequence_strings(item.get("artifact_refs")),
        "evidence_refs": text_or_sequence_strings(item.get("evidence_refs")),
    }
    requirement_id = _clean_id(item.get("requirement_id") or item.get("requirement"))
    if requirement_id:
        record["requirement_id"] = requirement_id
    return record


# LLM: 批量从被覆盖的 ID 列表构建初始覆盖条目。
# 函数用途: 根据 ID 列表批量生成覆盖条目。
def _records_from_covered_ids(value: object, coverer: str) -> list[dict[str, object]]:
    if not coverer:
        return []
    return [
        {
            "covered_run_id": covered,
            "covered_by_run_id": coverer,
            "reason": "",
            "artifact_refs": [],
            "evidence_refs": [],
        }
        for covered in text_or_sequence_strings(value)
    ]


# LLM: 检查单条覆盖记录是否能解决目标 run 的覆盖需求。
# 函数用途: 判定单条覆盖记录有效性。
def _record_resolves_run(target: str, record: dict[str, object], is_verified: Callable[[str], bool]) -> bool:
    if _clean_id(record.get("covered_run_id")) != target:
        return False
    return is_verified(_clean_id(record.get("covered_by_run_id")))


# LLM: 提取任务对象字典属性，兼容 dict 和类对象。
# 函数用途: 安全读取任务 attributes。
def _attributes_dict(task: object) -> dict[str, object]:
    attrs = task.get("attributes") if isinstance(task, dict) else getattr(task, "attributes", {})
    return attrs if isinstance(attrs, dict) else {}


# LLM: 对覆盖记录执行三元组去重。
# 函数用途: 覆盖记录去重。
def _dedupe_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        key = (
            _clean_id(record.get("covered_run_id")),
            _clean_id(record.get("covered_by_run_id")),
            str(record.get("reason") or ""),
        )
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


# LLM: 安全清理字符串标识符。
# 函数用途: 去除空字符并转为纯净文本。
def _clean_id(value: object) -> str:
    return str(value or "").strip()


# LLM: 从任务属性中读取关联的需求标识列表。
# 函数用途: 获取任务关联的需求 ID。
def task_requirement_ids(task: object) -> list[str]:
    attrs = _attributes_dict(task)
    raw = attrs.get("requirement_ids") or attrs.get("requirement_id")
    return text_or_sequence_strings(raw)


# LLM: 将需求标识列表合并并去重写入任务属性。
# 函数用途: 更新任务关联的需求 ID。
def merge_task_requirement_ids(task: object, requirement_ids: list[str]) -> list[str]:
    current = task_requirement_ids(task)
    new_ids = text_or_sequence_strings(requirement_ids)
    merged = sorted(set(current) | set(new_ids))
    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        attrs = {}
        task.attributes = attrs
    attrs["requirement_ids"] = merged
    return merged
