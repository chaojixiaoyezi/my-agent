# LLM: Compact failed acceptance-test facts for parent agents without exposing artifact bodies.
# 模块用途: 从 test_execution.json 提取可行动失败摘要，让父级/修复子代理知道具体该修哪里。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DETAIL_KEYS = (
    "missing_required_files",
    "placeholder_hits",
    "broken_local_refs",
    "inert_control_hits",
    "form_binding_hits",
    "missing_dom_id_hits",
)
_MAX_DETAIL_ITEMS = 8
_MAX_DETAIL_CHARS = 260


# LLM: acceptance_test_failure_payload returns bounded, refs-only failure hints for dispatch records.
# 函数用途: 读取测试报告中的失败项，输出一行 summary 和少量 details；读取失败时返回空字段。
def acceptance_test_failure_payload(test_ref: str | Path) -> dict[str, object]:
    path = Path(test_ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("records") if isinstance(payload, dict) else []
    summaries: list[str] = []
    details: list[str] = []
    for record in records if isinstance(records, list) else []:
        if not _record_failed(record):
            continue
        summary = _record_summary(record)
        if summary:
            summaries.append(summary)
        details.extend(_record_details(record))
    if not summaries and not details:
        return {}
    return {
        "parent_acceptance_test_failure_summary": "; ".join(summaries[:3]),
        "parent_acceptance_test_failure_details": details[:_MAX_DETAIL_ITEMS],
    }


# LLM: _record_failed normalizes old and new execution-record pass/fail shapes.
# 函数用途: 判断单条测试记录是否失败；未执行、exit_code 非 0、validation_result.ok=false 都算失败。
def _record_failed(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    validation = record.get("validation_result")
    return (
        record.get("executed") is not True
        or int(record.get("exit_code") or 0) != 0
        or (isinstance(validation, dict) and validation.get("ok") is False)
    )


# LLM: _record_summary keeps the first-line failure reason short enough for model tool results.
# 函数用途: 生成单条失败测试的摘要，例如 `inferred static site check: inert_control_hits=15`。
def _record_summary(record: dict[str, Any]) -> str:
    name = str(record.get("test_name") or record.get("validation_method") or "test").strip()
    error = str(record.get("error") or "").strip()
    if not error:
        error = _validation_count_summary(record.get("validation_result"))
    return _trim_detail(f"{name}: {error}" if error else name)


# LLM: _record_details exposes concrete broken refs/buttons without opening generated files.
# 函数用途: 从 validation_result 里提取具体失败列表，供父级生成修复派工提示。
def _record_details(record: dict[str, Any]) -> list[str]:
    validation = record.get("validation_result")
    if not isinstance(validation, dict):
        return []
    details: list[str] = []
    for key in _DETAIL_KEYS:
        items = validation.get(key)
        if not isinstance(items, list) or not items:
            continue
        detail = f"{key}: {'; '.join(str(item) for item in items[:_MAX_DETAIL_ITEMS])}"
        details.append(_trim_detail(detail))
    return details


# LLM: _validation_count_summary gives a fallback when record.error was omitted.
# 函数用途: 把 validation_result 的非空问题列表转成计数字符串，保持 summary 可读。
def _validation_count_summary(validation: Any) -> str:
    if not isinstance(validation, dict):
        return ""
    parts = [
        f"{key}={len(value)}"
        for key in _DETAIL_KEYS
        if isinstance((value := validation.get(key)), list) and value
    ]
    return "; ".join(parts)


# LLM: _trim_detail bounds each detail line so dispatch payloads stay cheap and safe.
# 函数用途: 对摘要或 detail 做统一长度限制；超长时保留前缀并加省略号。
def _trim_detail(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= _MAX_DETAIL_CHARS:
        return cleaned
    return f"{cleaned[:_MAX_DETAIL_CHARS - 3]}..."
