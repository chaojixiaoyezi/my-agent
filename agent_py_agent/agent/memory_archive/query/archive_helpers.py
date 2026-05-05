from __future__ import annotations

"""LLM: helper and utility functions for memory archive query operations.

新手说明:
这个文件放的是归档查询里的"小工具"——推导字段、拼预览、错误记录、搜索文本、
ID 提取、去重、时间解析、日期判断、列表归一化。
它们不直接读写文件，只是对数据做纯计算变换。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _derived_archive_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """LLM: derive request/run/task/status/source fields from hook internals.

    新手说明:
    hook snapshot 没有顶层 request_id/run_id。
    这些字段通常藏在 `turn_range` 或 `dispatch_events` 里，恢复搜索时要提出来，否则 `--run-id` 会漏掉 hook。

    参数说明:
    `payload` 是 hook/raw 原始记录。

    返回说明:
    返回推导出的 request/run/task/status/source 字段字典。
    """

    fields: dict[str, Any] = {}
    turn_range = payload.get("turn_range")
    if isinstance(turn_range, dict):
        _copy_present_archive_fields(fields, turn_range)
    dispatch_events = payload.get("dispatch_events")
    if isinstance(dispatch_events, list):
        for event in dispatch_events:
            if not isinstance(event, dict):
                continue
            _copy_present_archive_fields(fields, event, only_missing=True)
    return fields


def _copy_present_archive_fields(
    fields: dict[str, Any],
    source: dict[str, Any],
    *,
    only_missing: bool = False,
) -> None:
    """Copy known archive routing fields from a nested payload."""
    for key in ("request_id", "run_id", "task_id", "source", "status", "error_code"):
        if source.get(key) and (not only_missing or not fields.get(key)):
            fields[key] = source.get(key)


def _archive_preview(payload: dict[str, Any]) -> str:
    """LLM: derive a compact human-readable preview from raw or hook payloads.

    新手说明:
    不同类型的归档正文位置不一样。
    这里优先拿 content_preview；如果没有，就从意图、动作、决策、下一步里拼一个短摘要。

    参数说明:
    `payload` 是原始归档记录。

    返回说明:
    返回最多 500 字符的预览文本。
    """

    preview = str(payload.get("content_preview", "") or "").strip()
    if preview:
        return preview
    parts: list[str] = []
    for key in ("user_intents", "assistant_actions", "decisions", "open_questions", "next_actions", "task_refs"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return "；".join(parts)[:500]


def _archive_error_record(layer: str, path: Path, *, line_no: int, message: str) -> dict[str, Any]:
    """LLM: represent malformed archive lines as searchable diagnostic records.

    新手说明:
    坏行也算一种线索。
    用户至少应该知道哪个文件第几行坏了，而不是看到命令静悄悄漏掉内容。

    参数说明:
    `layer` 是 raw/hook；`path` 是出错文件；`line_no` 是行号；`message` 是错误说明。

    返回说明:
    返回一条标准化错误记录。
    """

    return {
        "layer": layer,
        "kind": "archive_error",
        "id": f"{path.name}:{line_no}:error",
        "session_id": "",
        "request_id": "",
        "run_id": "",
        "task_id": "",
        "speaker": "",
        "target": "",
        "action": "parse_error",
        "status": "failed",
        "error_code": "archive_json_decode_error",
        "is_dispatch": False,
        "tool_name": "",
        "tool_success": None,
        "source": "",
        "created_at": "",
        "created_at_sort": path.stat().st_mtime if path.exists() else 0.0,
        "content_preview": message,
        "content_path": "",
        "content_hash": "",
        "task_refs": [],
        "next_actions": [],
        "file_path": str(path),
        "line_no": line_no,
        "payload": {"error": message},
    }


def _archive_search_text(record: dict[str, Any]) -> str:
    """LLM: build the searchable text blob for one normalized archive record.

    新手说明:
    关键词不只搜正文预览，也搜 ID、工具名、状态、任务引用和原始 payload。
    这样用户记得某个 request_id 时也能找回来。

    参数说明:
    `record` 是标准化归档记录。

    返回说明:
    返回用于小写关键词匹配的大文本。
    """

    parts = [
        str(record.get("id", "")),
        str(record.get("session_id", "")),
        str(record.get("request_id", "")),
        str(record.get("run_id", "")),
        str(record.get("task_id", "")),
        str(record.get("speaker", "")),
        str(record.get("target", "")),
        str(record.get("action", "")),
        str(record.get("status", "")),
        str(record.get("tool_name", "")),
        str(record.get("source", "")),
        str(record.get("content_preview", "")),
        " ".join(record.get("task_refs", []) or []),
        " ".join(record.get("next_actions", []) or []),
        json.dumps(record.get("payload", {}), ensure_ascii=False, sort_keys=True),
    ]
    return "\n".join(parts).lower()


def _append_run_id(items: list[str], value: object) -> None:
    """LLM: extract and append one subagent run ID from a loose text value.

    新手说明:
    有些地方存的是完整句子，比如"请看 subagent-xxx"。
    这个函数把里面真正的 `subagent-*` ID 挖出来，避免恢复时找不到任务目录。

    参数说明:
    `items` 是要追加的 ID 列表；`value` 是可能包含 subagent ID 的任意值。

    返回说明:
    不返回值；可能原地追加一个 ID。
    """

    text = str(value or "").strip()
    if not text or "subagent-" not in text:
        return
    run_id = text[text.find("subagent-") :].split()[0].strip("`'\",)")
    if run_id and run_id not in items:
        items.append(run_id)


def _dedupe_strings(values: list[str]) -> list[str]:
    """LLM: keep non-empty strings once while preserving order.

    新手说明:
    gateway 的 request_path、response_path、content_path 可能有空值，也可能重复。
    这里收成干净列表，给 Recovery Brief 和 CLI 输出直接使用。

    参数说明:
    `values` 是候选路径字符串列表。

    返回说明:
    返回去空、去重后的字符串列表。
    """

    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _created_at_sort(value: str, *, fallback: float) -> float:
    """LLM: convert an ISO-like timestamp to a sortable epoch value.

    新手说明:
    归档记录要按时间倒序显示。
    如果时间字符串坏了，就用文件修改时间这类 fallback，保证命令还能继续跑。

    参数说明:
    `value` 是 ISO-like 时间字符串；`fallback` 是解析失败时使用的时间戳。

    返回说明:
    返回 epoch 秒数。
    """

    text = str(value or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_date_only(value: str) -> bool:
    """LLM: detect `YYYY-MM-DD` filters that should mean the whole day.

    新手说明:
    用户写 `--until 2026-04-30` 时，通常想包含 4 月 30 日全天，而不是只到当天 00:00。
    这个 helper 只识别最朴素的日期格式，让 `filter_archive_records()` 可以把 until 推到当天末尾。

    参数说明:
    `value` 是用户传入的 since/until 字符串。

    返回说明:
    形如 `YYYY-MM-DD` 时返回 True，否则 False。
    """

    text = str(value or "").strip()
    if len(text) != 10:
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return text[4] == "-" and text[7] == "-"


def _list_value(value: object) -> list[object]:
    """LLM: normalize a scalar-or-list payload field into a list.

    新手说明:
    JSON 里有些字段可能是单个字符串，也可能已经是列表。
    这里统一成列表，后面搜索和展示就不用到处判断类型。

    参数说明:
    `value` 是待归一化字段。

    返回说明:
    返回列表；空值返回空列表。
    """

    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]
