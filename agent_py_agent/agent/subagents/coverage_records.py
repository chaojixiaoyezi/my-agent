# LLM: Coverage records let parent/coordination runs prove that a failed child was covered by a verified sibling.
# 模块用途: 规范化“哪个 run 覆盖了哪个失败 run”的机器字段，供验收、看板和最终收口复用。

from __future__ import annotations

from collections.abc import Callable
from typing import Any


# LLM: coverage_records_from_payload accepts stable field names plus narrow aliases for model output.
# 函数用途: 从 runner JSON 里提取覆盖记录；要求 covered_run_id 和 covered_by_run_id 都明确，避免口头 fallback 被误认。
def coverage_records_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    records = [
        *normalize_coverage_records(payload.get("coverage_records")),
        *normalize_coverage_records(payload.get("coverage")),
    ]
    top_level_coverer = _clean_id(payload.get("covered_by_run_id") or payload.get("covering_run_id"))
    records.extend(_records_from_covered_ids(payload.get("covered_run_ids"), top_level_coverer))
    return _dedupe_records(records)


# LLM: normalize_coverage_records keeps coverage evidence small and run-id grounded.
# 函数用途: 只保留 run id、原因和 refs，不读取产物正文，也不接受缺少覆盖者的记录。
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


# LLM: merge_task_coverage_records persists normalized records on task.attributes.
# 函数用途: 把当前 runner 声明的覆盖关系写入任务属性，后续验收/收口不再解析自然语言说明。
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


# LLM: task_coverage_records reads persisted coverage without assuming a concrete dataclass.
# 函数用途: 从 SubAgentTask、MagicMock 或 task.json dict 中读取 coverage_records。
def task_coverage_records(task: object) -> list[dict[str, object]]:
    attrs = _attributes_dict(task)
    return normalize_coverage_records(attrs.get("coverage_records"))


# LLM: all_task_coverage_records collects coverage assertions from the current in-memory task scope only.
# 函数用途: 汇总当前任务列表里的覆盖关系；不扫描磁盘，不跨工作区。
def all_task_coverage_records(tasks: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for task in tasks:
        records.extend(task_coverage_records(task))
    return _dedupe_records(records)


# LLM: coverage_records_resolve_run is the shared predicate for closeout and descendant health.
# 函数用途: 只有 covered_by_run_id 指向已 VERIFIED 的任务时，才把 covered_run_id 视为已覆盖。
def coverage_records_resolve_run(
    run_id: str,
    records: list[dict[str, object]],
    is_verified: Callable[[str], bool],
) -> bool:
    target = _clean_id(run_id)
    if not target:
        return False
    return any(_record_resolves_run(target, record, is_verified) for record in records)


# LLM: _coverage_record_from_item normalizes one record and drops vague coverage claims.
# 函数用途: 兼容少量别名，但不从 reason/summary 自然语言里猜 run id。
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
    return {
        "covered_run_id": covered,
        "covered_by_run_id": coverer,
        "reason": str(item.get("reason") or item.get("summary") or "").strip(),
        "artifact_refs": _string_list(item.get("artifact_refs")),
        "evidence_refs": _string_list(item.get("evidence_refs")),
    }


# LLM: _records_from_covered_ids supports compact payloads only when a top-level coverer is explicit.
# 函数用途: 允许 {"covered_by_run_id":"x","covered_run_ids":["y"]} 这种短格式。
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
        for covered in _string_list(value)
    ]


# LLM: _record_resolves_run keeps the verification check injected by each caller's state store.
# 函数用途: 判断一条覆盖记录是否能证明目标 run 已被已验证 run 覆盖。
def _record_resolves_run(target: str, record: dict[str, object], is_verified: Callable[[str], bool]) -> bool:
    if _clean_id(record.get("covered_run_id")) != target:
        return False
    return is_verified(_clean_id(record.get("covered_by_run_id")))


# LLM: _attributes_dict handles task objects and persisted JSON records uniformly.
# 函数用途: 从对象或 dict 中安全读取 attributes 字典。
def _attributes_dict(task: object) -> dict[str, object]:
    attrs = task.get("attributes") if isinstance(task, dict) else getattr(task, "attributes", {})
    return attrs if isinstance(attrs, dict) else {}


# LLM: _dedupe_records preserves first-seen records for stable JSON output.
# 函数用途: 按 covered/coverer/reason 去重，避免多轮 runner 结果重复膨胀。
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


# LLM: _string_list normalizes compact refs without accepting nested bodies.
# 函数用途: 将 refs 字段转成字符串列表；只处理 string/list，不展开 dict 正文。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


# LLM: _clean_id keeps run identifiers literal and compact.
# 函数用途: 清理 run id 字符串；空值返回空字符串。
def _clean_id(value: object) -> str:
    return str(value or "").strip()
