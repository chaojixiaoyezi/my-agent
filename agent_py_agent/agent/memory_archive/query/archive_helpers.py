# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""helper and utility functions for memory archive query operations.

新手说明:
这个文件放的是归档查询里的"小工具"——推导字段、拼预览、错误记录、搜索文本、
ID 提取、去重、时间解析、日期判断、列表归一化。
它们不直接读写文件，只是对数据做纯计算变换。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _derived_archive_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 derived archive fields 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _derived_archive_fields(payload: dict[str, Any]) -> dict[str, Any]:

    fields: dict[str, Any] = {}
    turn_range = payload.get("turn_range")
    if isinstance(turn_range, dict):
        _copy_present_archive_fields(fields, turn_range)
    dispatch_events = payload.get("dispatch_events")
    if isinstance(dispatch_events, list):
        _copy_dispatch_archive_fields(fields, dispatch_events)
    return fields


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _copy_dispatch_archive_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 copy dispatch archive fields 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _copy_dispatch_archive_fields(fields: dict[str, Any], dispatch_events: list[object]) -> None:
    for event in dispatch_events:
        if isinstance(event, dict):
            _copy_present_archive_fields(fields, event, only_missing=True)


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _copy_present_archive_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 copy present archive fields 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _archive_preview 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive preview 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _archive_preview(payload: dict[str, Any]) -> str:

    preview = str(payload.get("content_preview", "") or "").strip()
    if preview:
        return preview
    parts: list[str] = []
    for key in ("user_intents", "assistant_actions", "decisions", "open_questions", "next_actions", "task_refs"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return "；".join(parts)[:500]


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _archive_error_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive error record 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _archive_error_record(layer: str, path: Path, *, line_no: int, message: str) -> dict[str, Any]:

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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _archive_search_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive search text 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _archive_search_text(record: dict[str, Any]) -> str:

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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _append_run_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append run id 相关记录，集中处理目标路径、格式化和状态更新。
def _append_run_id(items: list[str], value: object) -> None:

    text = str(value or "").strip()
    if not text or "subagent-" not in text:
        return
    run_id = text[text.find("subagent-") :].split()[0].strip("`'\",)")
    if run_id and run_id not in items:
        items.append(run_id)


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _dedupe_strings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe strings 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe_strings(values: list[str]) -> list[str]:

    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _created_at_sort 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 created at sort 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _created_at_sort(value: str, *, fallback: float) -> float:

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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _is_date_only 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 is date only 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _is_date_only(value: str) -> bool:

    text = str(value or "").strip()
    if len(text) != 10:
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return text[4] == "-" and text[7] == "-"


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _list_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 list value 的候选结果，并按参数完成筛选、排序或数量限制。
def _list_value(value: object) -> list[object]:

    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]
